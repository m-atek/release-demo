"""A cluster in virtual time, for the Blip check: kubelet's probes on the pods, kube-proxy's Service,
and the policy, with the chart's real timing. It is the spec's reasoning as code, not a measurement."""

import blip_check

SECONDS = 30
READINESS = (5, 2)  # the chart's period and failure threshold
LIVENESS = (10, 3)
INITIAL_DELAY = 5
PRE_STOP = 5
STARTS_IN = 1


class Pod:
    def __init__(self, name, offset):
        self.name, self.offset = name, offset
        self.ready, self.restarts = True, 0
        self.running = True
        self.readiness_failures = self.liveness_failures = 0
        self.next_readiness = offset % READINESS[0]
        self.next_liveness = offset % LIVENESS[0]
        self.exits_at = self.starts_at = None
        self.on_hold = False  # kubelet stops liveness probes between a kill and the new container
        self.waiting = None


class Cluster:
    """version 1.10.0: both probes ask the database. 1.10.1: neither does."""

    def __init__(self, version="1.10.0", offsets=(1.3, 4.7, 8.2), apply_lag=0.3, delete_lag=0.3, replicas=3):
        self.version, self.now, self.apply_lag, self.delete_lag = version, 0.0, apply_lag, delete_lag
        self.pods = [Pod(f"catalog-5d9f8b6c5-pod{i}", offset) for i, offset in enumerate(offsets[:replicas])]
        self.policy, self.blocked_from, self.blocked_to = False, None, None
        self.events, self.turn = [], 0
        self.posted, self.deleted = [], []
        self.endpoints_lag = 0.5
        self.left_ready_at = {}

    # Time.

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.advance(self.now + seconds)

    def advance(self, until):
        while True:
            due = [(self.tick_time(pod), index) for index, pod in enumerate(self.pods)]
            first = min(due)
            if first[0] > until:
                break
            self.now = max(self.now, first[0])
            self.step(self.pods[first[1]])
        self.now = max(self.now, until)

    def tick_time(self, pod):
        return min(t for t in (pod.next_readiness, pod.next_liveness if not pod.on_hold else None, pod.exits_at,
                               pod.starts_at) if t is not None)

    def step(self, pod):
        now = self.tick_time(pod)
        if pod.exits_at == now:
            pod.exits_at, pod.running, pod.ready = None, False, False
            pod.starts_at = now + STARTS_IN
            self.left_ready_at[pod.name] = now
            return
        if pod.starts_at == now:
            pod.starts_at, pod.running, pod.on_hold = None, True, False
            pod.restarts += 1
            pod.readiness_failures = pod.liveness_failures = 0
            pod.next_readiness = now
            pod.next_liveness = now + INITIAL_DELAY
            return
        if pod.next_readiness == now:
            pod.next_readiness += READINESS[0]
            if not pod.running:
                return
            if self.version == "1.10.0" and not self.database_answers():
                pod.readiness_failures += 1
                if pod.readiness_failures >= READINESS[1] and pod.ready:
                    pod.ready = False
                    self.left_ready_at[pod.name] = now
            else:
                pod.readiness_failures = 0
                pod.ready = True
            return
        pod.next_liveness += LIVENESS[0]
        if not pod.running or pod.on_hold:
            return
        if self.version == "1.10.0" and not self.database_answers():
            pod.liveness_failures += 1
            if pod.liveness_failures >= LIVENESS[1]:
                pod.on_hold, pod.liveness_failures = True, 0
                pod.exits_at = now + PRE_STOP
                self.events.append({
                    "metadata": {"name": f"{pod.name}.killing"}, "involvedObject": {"name": pod.name},
                    "reason": "Killing", "count": pod.restarts + 1,
                    "message": "Container catalog failed liveness probe, will be restarted"})
        else:
            pod.liveness_failures = 0

    # The database and the Service.

    def database_answers(self):
        return not (self.blocked_from is not None and self.blocked_from <= self.now
                    and (self.blocked_to is None or self.now < self.blocked_to))

    def in_service(self):
        return [pod for pod in self.pods if pod.ready or self.now - self.left_ready_at.get(pod.name, -99) < self.endpoints_lag]

    def http(self, port, route):
        self.advance(self.now)
        pods = self.in_service()
        if not pods:
            return "refused", False
        self.turn += 1
        pod = pods[self.turn % len(pods)]
        if not pod.running:
            return "refused", False
        if route == "/health":
            return 200, False
        return (200, False) if self.database_answers() else (503, False)

    # The API.

    def pods_list(self, namespace, release):
        self.advance(self.now)
        return [{"metadata": {"name": pod.name}, "status": {"containerStatuses": [
            {"ready": pod.ready, "restartCount": pod.restarts,
             "state": {"waiting": {"reason": pod.waiting}} if pod.waiting else {"running": {}}}]}} for pod in self.pods]

    def api(self):
        return Api(self)


class Api:
    def __init__(self, cluster):
        self.cluster = cluster

    def pods(self, namespace, release):
        return self.cluster.pods_list(namespace, release)

    def get(self, path):
        assert "events" in path and "reason%3DKilling" in path, path
        return {"items": list(self.cluster.events)}

    def post(self, path, document):
        cluster = self.cluster
        cluster.posted.append((path, document))
        cluster.policy = True
        cluster.blocked_from = cluster.now + cluster.apply_lag
        cluster.blocked_to = None
        return document

    def delete(self, path):
        cluster = self.cluster
        cluster.deleted.append((cluster.now, path))
        was = cluster.policy
        if was:
            cluster.policy = False
            cluster.blocked_to = cluster.now + cluster.delete_lag
        return was


class Pool:
    """Runs each job at once: time passes only when the check sleeps."""

    def submit(self, job, *args):
        job(*args)

    def shutdown(self, wait=True, cancel_futures=False):
        pass


def config(**changes):
    values = {"service": "catalog", "environment": "staging", "namespace": "staging", "release": "catalog", "replicas": 3,
              "port": 30081, "seconds": SECONDS, "calls_per_second": 10, "data_route": "/api/products",
              "alive_route": "/health", "recover_seconds": 60}
    return blip_check.Settings(**{**values, **changes})


class Said(list):
    def __call__(self, text):
        self.append(text)


def check(cluster, http=None, version=None, settings=None):
    """The real Check on this cluster. Returns (the check, what it said)."""
    said = Said()
    return blip_check.Check(settings or config(), cluster.api(), version or cluster.version, said, call=http or cluster.http,
                            clock=cluster.clock, sleep=cluster.sleep, wall=lambda: 1_793_000_000.0, pool=Pool()), said
