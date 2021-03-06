#!/usr/bin/env python
# -*- coding: utf-8 -*-
# Copyright 2016 OpenMarket Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import synapse

from synapse.server import HomeServer
from synapse.config._base import ConfigError
from synapse.config.logger import setup_logging
from synapse.config.homeserver import HomeServerConfig
from synapse.http.site import SynapseSite
from synapse.metrics.resource import MetricsResource, METRICS_PREFIX
from synapse.storage.roommember import RoomMemberStore
from synapse.replication.slave.storage.events import SlavedEventStore
from synapse.replication.slave.storage.pushers import SlavedPusherStore
from synapse.replication.slave.storage.receipts import SlavedReceiptsStore
from synapse.replication.slave.storage.account_data import SlavedAccountDataStore
from synapse.replication.tcp.client import ReplicationClientHandler
from synapse.storage.engines import create_engine
from synapse.storage import DataStore
from synapse.util.httpresourcetree import create_resource_tree
from synapse.util.logcontext import LoggingContext, preserve_fn, \
    PreserveLoggingContext
from synapse.util.manhole import manhole
from synapse.util.rlimit import change_resource_limit
from synapse.util.versionstring import get_version_string

from synapse import events

from twisted.internet import reactor, defer
from twisted.web.resource import Resource

from daemonize import Daemonize

import sys
import logging
import gc

logger = logging.getLogger("synapse.app.pusher")


class PusherSlaveStore(
    SlavedEventStore, SlavedPusherStore, SlavedReceiptsStore,
    SlavedAccountDataStore
):
    update_pusher_last_stream_ordering_and_success = (
        DataStore.update_pusher_last_stream_ordering_and_success.__func__
    )

    update_pusher_failing_since = (
        DataStore.update_pusher_failing_since.__func__
    )

    update_pusher_last_stream_ordering = (
        DataStore.update_pusher_last_stream_ordering.__func__
    )

    get_throttle_params_by_room = (
        DataStore.get_throttle_params_by_room.__func__
    )

    set_throttle_params = (
        DataStore.set_throttle_params.__func__
    )

    get_time_of_last_push_action_before = (
        DataStore.get_time_of_last_push_action_before.__func__
    )

    get_profile_displayname = (
        DataStore.get_profile_displayname.__func__
    )

    who_forgot_in_room = (
        RoomMemberStore.__dict__["who_forgot_in_room"]
    )


class PusherServer(HomeServer):
    def get_db_conn(self, run_new_connection=True):
        # Any param beginning with cp_ is a parameter for adbapi, and should
        # not be passed to the database engine.
        db_params = {
            k: v for k, v in self.db_config.get("args", {}).items()
            if not k.startswith("cp_")
        }
        db_conn = self.database_engine.module.connect(**db_params)

        if run_new_connection:
            self.database_engine.on_new_connection(db_conn)
        return db_conn

    def setup(self):
        logger.info("Setting up.")
        self.datastore = PusherSlaveStore(self.get_db_conn(), self)
        logger.info("Finished setting up.")

    def remove_pusher(self, app_id, push_key, user_id):
        self.get_tcp_replication().send_remove_pusher(app_id, push_key, user_id)

    def _listen_http(self, listener_config):
        port = listener_config["port"]
        bind_addresses = listener_config["bind_addresses"]
        site_tag = listener_config.get("tag", port)
        resources = {}
        for res in listener_config["resources"]:
            for name in res["names"]:
                if name == "metrics":
                    resources[METRICS_PREFIX] = MetricsResource(self)

        root_resource = create_resource_tree(resources, Resource())

        for address in bind_addresses:
            reactor.listenTCP(
                port,
                SynapseSite(
                    "synapse.access.http.%s" % (site_tag,),
                    site_tag,
                    listener_config,
                    root_resource,
                ),
                interface=address
            )

        logger.info("Synapse pusher now listening on port %d", port)

    def start_listening(self, listeners):
        for listener in listeners:
            if listener["type"] == "http":
                self._listen_http(listener)
            elif listener["type"] == "manhole":
                bind_addresses = listener["bind_addresses"]

                for address in bind_addresses:
                    reactor.listenTCP(
                        listener["port"],
                        manhole(
                            username="matrix",
                            password="rabbithole",
                            globals={"hs": self},
                        ),
                        interface=address
                    )
            else:
                logger.warn("Unrecognized listener type: %s", listener["type"])

        self.get_tcp_replication().start_replication(self)

    def build_tcp_replication(self):
        return PusherReplicationHandler(self)


class PusherReplicationHandler(ReplicationClientHandler):
    def __init__(self, hs):
        super(PusherReplicationHandler, self).__init__(hs.get_datastore())

        self.pusher_pool = hs.get_pusherpool()

    def on_rdata(self, stream_name, token, rows):
        super(PusherReplicationHandler, self).on_rdata(stream_name, token, rows)
        preserve_fn(self.poke_pushers)(stream_name, token, rows)

    @defer.inlineCallbacks
    def poke_pushers(self, stream_name, token, rows):
        if stream_name == "pushers":
            for row in rows:
                if row.deleted:
                    yield self.stop_pusher(row.user_id, row.app_id, row.pushkey)
                else:
                    yield self.start_pusher(row.user_id, row.app_id, row.pushkey)
        elif stream_name == "events":
            yield self.pusher_pool.on_new_notifications(
                token, token,
            )
        elif stream_name == "receipts":
            yield self.pusher_pool.on_new_receipts(
                token, token, set(row.room_id for row in rows)
            )

    def stop_pusher(self, user_id, app_id, pushkey):
        key = "%s:%s" % (app_id, pushkey)
        pushers_for_user = self.pusher_pool.pushers.get(user_id, {})
        pusher = pushers_for_user.pop(key, None)
        if pusher is None:
            return
        logger.info("Stopping pusher %r / %r", user_id, key)
        pusher.on_stop()

    def start_pusher(self, user_id, app_id, pushkey):
        key = "%s:%s" % (app_id, pushkey)
        logger.info("Starting pusher %r / %r", user_id, key)
        return self.pusher_pool._refresh_pusher(app_id, pushkey, user_id)


def start(config_options):
    try:
        config = HomeServerConfig.load_config(
            "Synapse pusher", config_options
        )
    except ConfigError as e:
        sys.stderr.write("\n" + e.message + "\n")
        sys.exit(1)

    assert config.worker_app == "synapse.app.pusher"

    setup_logging(config, use_worker_options=True)

    events.USE_FROZEN_DICTS = config.use_frozen_dicts

    if config.start_pushers:
        sys.stderr.write(
            "\nThe pushers must be disabled in the main synapse process"
            "\nbefore they can be run in a separate worker."
            "\nPlease add ``start_pushers: false`` to the main config"
            "\n"
        )
        sys.exit(1)

    # Force the pushers to start since they will be disabled in the main config
    config.start_pushers = True

    database_engine = create_engine(config.database_config)

    ps = PusherServer(
        config.server_name,
        db_config=config.database_config,
        config=config,
        version_string="Synapse/" + get_version_string(synapse),
        database_engine=database_engine,
    )

    ps.setup()
    ps.start_listening(config.worker_listeners)

    def run():
        # make sure that we run the reactor with the sentinel log context,
        # otherwise other PreserveLoggingContext instances will get confused
        # and complain when they see the logcontext arbitrarily swapping
        # between the sentinel and `run` logcontexts.
        with PreserveLoggingContext():
            logger.info("Running")
            change_resource_limit(config.soft_file_limit)
            if config.gc_thresholds:
                gc.set_threshold(*config.gc_thresholds)
            reactor.run()

    def start():
        ps.get_pusherpool().start()
        ps.get_datastore().start_profiling()
        ps.get_state_handler().start_caching()

    reactor.callWhenRunning(start)

    if config.worker_daemonize:
        daemon = Daemonize(
            app="synapse-pusher",
            pid=config.worker_pid_file,
            action=run,
            auto_close_fds=False,
            verbose=True,
            logger=logger,
        )
        daemon.start()
    else:
        run()


if __name__ == '__main__':
    with LoggingContext("main"):
        ps = start(sys.argv[1:])
