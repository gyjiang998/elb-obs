"""Readable summaries alongside the complete machine-readable DAG.

Solid arrows describe execution/dependency order. Dashed arrows only summarize
observed IPC ownership; the exact request/reply edges remain in graph.edges.
"""

from collections import Counter


def _label(value):
    return str(value).replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", " ")


def annotate(graph, extracted):
    indexed = {node["id"]: node for node in graph["nodes"]}
    components = {item["id"]: item for item in extracted["components"]}
    lines = ["flowchart LR"]
    identities = {}
    groups = []

    def add(identity, label, kind):
        token = f"n{len(identities)}"
        identities[identity] = token
        lines.append(f'{token}["{_label(label)}"]:::{kind}')
        return token

    if graph["variant"] == "observed_serial":
        chunks = []
        for identity in extracted["app_order"]:
            node = indexed[identity]
            component = node.get("component")
            if not chunks or component != chunks[-1]["component"]:
                chunks.append({"component": component, "nodes": [], "wall_ms": 0.0, "cpu_ms": 0.0})
            chunk = chunks[-1]
            chunk["nodes"].append(identity)
            chunk["wall_ms"] += node.get("observed_duration_ms", 0)
            chunk["cpu_ms"] += node["duration_ms"] if node["kind"] == "cpu" else 0
        owner = {}
        for index, chunk in enumerate(chunks):
            identity = f"chunk_{index}"
            label = components[chunk["component"]]["name"] if chunk["component"] else "主线程保留工作"
            count = len(components[chunk["component"]]["request_ids"]) if chunk["component"] else 0
            suffix = f" · IPC {count} 次" if count else ""
            add(identity, f'{label} · {chunk["wall_ms"]:.3f} ms / CPU {chunk["cpu_ms"]:.3f} ms{suffix}', "build" if chunk["component"] else "main")
            groups.append(identity)
            owner.update({node_id: identity for node_id in chunk["nodes"]})
        for previous, current in zip(groups, groups[1:]):
            lines.append(f"{identities[previous]} --> {identities[current]}")
    else:
        owner = {identity: node.get("group") for identity, node in indexed.items()}
        order = ["main_pre", "main_residual", *sorted(identity for identity in graph["groups"] if identity.startswith("build_")), "main_post"]
        for identity in order:
            if identity not in graph["groups"]:
                continue
            group = graph["groups"][identity]
            label = {"main_pre": "主线程前段", "main_residual": "主线程保留工作", "main_post": "主线程后段"}.get(identity, group["label"])
            count = len(components[identity]["request_ids"]) if identity in components else 0
            suffix = f" · IPC {count} 次" if count else ""
            add(identity, f'{label} · 参考 CPU {group["cpu_ms"]:.3f} ms{suffix}', "build" if group["branch"] else "main")
            groups.append(identity)
        fork = add("fork", "条件 fork：所选输入就绪", "gate")
        join = add("join", "join：等待所有结果", "gate")
        lines.extend([f'{identities["main_pre"]} --> {fork}', f'{join} --> {identities["main_post"]}'])
        for identity in groups:
            if identity not in ("main_pre", "main_post"):
                lines.extend([f'{fork} --> {identities[identity]}', f'{identities[identity]} --> {join}'])
    calls = Counter()
    service_names = {}
    for request in extracted["requests"]:
        group = owner.get(f'submit_{request["id"]}')
        if group:
            calls[(group, request["resource"])] += 1
            service_names[request["resource"]] = request["service"]
    for resource, name in sorted(service_names.items()):
        count = sum(value for (group, target), value in calls.items() if target == resource)
        add(resource, f"{name} · {count} 次真实调用", "service")
    for (group, resource), count in sorted(calls.items()):
        lines.append(f'{identities[group]} -. "{count} 次调用归属" .-> {identities[resource]}')
    lines.extend([
        'legend["实线：顺序 / fork / join；虚线：服务调用归属，不能当作额外依赖。精确请求与返回边见完整 DAG。"]:::note',
        "classDef main fill:#fee2e2,stroke:#b91c1c,color:#111827",
        "classDef build fill:#d1fae5,stroke:#047857,color:#111827",
        "classDef service fill:#fef3c7,stroke:#b45309,color:#111827",
        "classDef gate fill:#dbeafe,stroke:#1d4ed8,color:#111827",
        "classDef note fill:#f3f4f6,stroke:#6b7280,color:#111827",
    ])
    graph["mermaid"] = "\n".join(lines)
    graph["visualization_scope"] = "Summary of original ordering / conditional groups and IPC ownership; complete dependencies are retained in nodes and edges. CPU labels are measured reference work, not per-lane predicted duration."
    return graph
