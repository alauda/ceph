import logging
import signal
import time

from gevent import sleep
from gevent.greenlet import Greenlet
from gevent.event import Event

log = logging.getLogger(__name__)

class DaemonWatchdog(Greenlet):
    """
    DaemonWatchdog::

    Watch Ceph daemons for failures. If an extended failure is detected (i.e.
    not intentional), then the watchdog will unmount file systems and send
    SIGTERM to all daemons. The duration of an extended failure is configurable
    with watchdog_daemon_timeout.

    watchdog_daemon_timeout [default: 300]: number of seconds a daemon
        is allowed to be failed before the watchdog will bark.
    """

    def __init__(self, ctx, manager, config, thrashers):
        Greenlet.__init__(self)
        self.ctx = ctx
        self.config = config
        self.e = None
        self.logger = log.getChild('daemon_watchdog')
        self.manager = manager
        self.name = 'watchdog'
        self.stopping = Event()
        self.thrashers = thrashers

    def _run(self):
        try:
            self.watch()
        except Exception as e:
            # See _run exception comment for MDSThrasher
            self.e = e
            self.logger.exception("exception:")
            # allow successful completion so gevent doesn't see an exception...

    def log(self, x):
        """Write data to logger"""
        self.logger.info(x)

    def stop(self):
        self.stopping.set()

    def bark(self):
        self.log("BARK! unmounting mounts and killing all daemons")
        for mount in self.ctx.mounts.values():
            try:
                mount.umount_wait(force=True)
            except:
                self.logger.exception("ignoring exception:")
        daemons = []
        daemons.extend(filter(lambda daemon: daemon.running() and not daemon.proc.finished, self.ctx.daemons.iter_daemons_of_role('mds', cluster=self.manager.cluster)))
        daemons.extend(filter(lambda daemon: daemon.running() and not daemon.proc.finished, self.ctx.daemons.iter_daemons_of_role('mon', cluster=self.manager.cluster)))
        for daemon in daemons:
            try:
                daemon.signal(signal.SIGTERM)
            except:
                self.logger.exception("ignoring exception:")

    def watch(self):
        self.log("watchdog starting")
        daemon_timeout = int(self.config.get('watchdog_daemon_timeout', 300))
        daemon_failure_time = {}
        while not self.stopping.is_set():
            bark = False
            now = time.time()

            mons = self.ctx.daemons.iter_daemons_of_role('mon', cluster=self.manager.cluster)
            mdss = self.ctx.daemons.iter_daemons_of_role('mds', cluster=self.manager.cluster)

            #for daemon in mons:
            #    self.log("mon daemon {role}.{id}: running={r}".format(role=daemon.role, id=daemon.id_, r=daemon.running() and not daemon.proc.finished))
            #for daemon in mdss:
            #    self.log("mds daemon {role}.{id}: running={r}".format(role=daemon.role, id=daemon.id_, r=daemon.running() and not daemon.proc.finished))

            daemon_failures = []
            daemon_failures.extend(filter(lambda daemon: daemon.running() and daemon.proc.finished, mons))
            daemon_failures.extend(filter(lambda daemon: daemon.running() and daemon.proc.finished, mdss))
            for daemon in daemon_failures:
                name = daemon.role + '.' + daemon.id_
                dt = daemon_failure_time.setdefault(name, (daemon, now))
                assert dt[0] is daemon
                delta = now-dt[1]
                self.log("daemon {name} is failed for ~{t:.0f}s".format(name=name, t=delta))
                if delta > daemon_timeout:
                    bark = True

            # If a daemon is no longer failed, remove it from tracking:
            for name in daemon_failure_time.keys():
                if name not in [d.role + '.' + d.id_ for d in daemon_failures]:
                    self.log("daemon {name} has been restored".format(name=name))
                    del daemon_failure_time[name]

            for thrasher in self.thrashers:
                if thrasher.e is not None:
                    self.log("thrasher on fs.{name} failed".format(name=thrasher.fs.name))
                    bark = True

            if bark:
                self.bark()
                return

            sleep(5)

        self.log("watchdog finished")
