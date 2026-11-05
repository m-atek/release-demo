"""A node as kubectl and the kubelet would answer for it, with this cluster's real requests by default.

    node = FakeNode()
    room.read(node.get)
"""

MIB = 1 << 20
SYSTEM = [("kube-system", "coredns-5b4f7c9d8c-x2x9q", 70), ("monitoring", "kube-state-metrics-6d8f7c9b5d-k7k2p", 32)]
ALLOCATABLE_KI = 1208 * 1000 + 512  # about 1,180.2 MiB, as the kubelet would report it


def pod(namespace, name, request_mib, phase="Running", node="tools-host"):
    return {"metadata": {"namespace": namespace, "name": name, "uid": f"uid-{name}"},
            "spec": {"nodeName": node, "containers": [{"resources": {"requests": {"memory": f"{request_mib}Mi"}}}]},
            "status": {"phase": phase}}


class FakeNode:
    def __init__(self, forecast_mib=256, catalog_mib=48, pending=None, said="", used_mib=256, forecast_used_mib=51):
        self.pods = [pod(namespace, name, request) for namespace, name, request in SYSTEM]
        for namespace in ("staging", "production"):
            self.pods += [pod(namespace, f"catalog-6f7d9c8b7-{i}", catalog_mib) for i in range(3)]
        self.pods += [pod("forecast", f"forecast-7d9f8b6c5-{i}", forecast_mib) for i in range(3)]
        self.catalog_mib, self.used_mib, self.forecast_used_mib, self.events = catalog_mib, used_mib, forecast_used_mib, []
        if pending:
            waiting = pod("staging", pending, catalog_mib, phase="Pending", node=None)
            del waiting["spec"]["nodeName"]
            self.pods.append(waiting)
            self.events.append({"metadata": {"creationTimestamp": "2026-11-20T10:00:05Z"},
                                "involvedObject": {"name": pending}, "message": said})

    def summary(self):
        return {"node": {"systemContainers": [
                    {"name": "kubelet", "memory": {"usageBytes": 400 * MIB}},
                    {"name": "pods", "memory": {"usageBytes": self.used_mib * MIB, "workingSetBytes": (self.used_mib - 60) * MIB}}]},
                "pods": [{"podRef": {"namespace": "forecast", "name": f"forecast-{i}"},
                          "memory": {"usageBytes": self.forecast_used_mib * MIB // 3}} for i in range(3)]
                + [{"podRef": {"namespace": "staging", "name": "catalog-0"}, "memory": {"usageBytes": 20 * MIB}}]}

    def get(self, *args):
        if args[:2] == ("get", "node"):
            return {"status": {"allocatable": {"memory": f"{ALLOCATABLE_KI}Ki"}}}
        if args[:2] == ("get", "pods"):
            return {"items": self.pods}
        if args[:2] == ("get", "--raw"):
            return self.summary()
        if args[:2] == ("get", "events"):
            assert "reason=FailedScheduling" in args
            return {"items": self.events}
        if args[:2] == ("get", "deployment"):
            return {"spec": {"template": {"spec": {"containers": [{"resources": {"requests": {"memory": f"{self.catalog_mib}Mi"}}}]}}}}
        raise AssertionError(f"kubectl {args}")
