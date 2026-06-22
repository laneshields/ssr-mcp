import json

from ssr_mcp.core import mcp, get_client


@mcp.resource(
    "ssr://routers",
    description="List of all routers managed by this conductor (or the local router in direct-connect mode)",
    mime_type="application/json",
)
async def resource_routers() -> str:
    return json.dumps(await get_client().get_routers(), indent=2)


@mcp.resource(
    "ssr://routers/{router}/nodes",
    description="List of nodes on a specific router",
    mime_type="application/json",
)
async def resource_router_nodes(router: str) -> str:
    return json.dumps(await get_client().get_router_nodes(router), indent=2)


@mcp.resource(
    "ssr://routers/{router}/nodes/{node}/device-interfaces",
    description="Device interfaces on a router node, including operational state and bandwidth analytics",
    mime_type="application/json",
)
async def resource_device_interfaces(router: str, node: str) -> str:
    return json.dumps(await get_client().get_device_interfaces(router, node), indent=2)


@mcp.resource(
    "ssr://routers/{router}/nodes/{node}/device-interfaces/{device_interface}/network-interfaces",
    description="Network interfaces attached to a specific device interface",
    mime_type="application/json",
)
async def resource_network_interfaces(router: str, node: str, device_interface: str) -> str:
    data = await get_client().get_network_interfaces(router, node)
    matches = []
    for r in ((data.get("data") or {}).get("allRouters") or {}).get("nodes") or []:
        for n in (r.get("nodes") or {}).get("nodes") or []:
            for di in (n.get("deviceInterfaces") or {}).get("nodes") or []:
                for ni in (di.get("networkInterfaces") or {}).get("nodes") or []:
                    if (ni.get("deviceInterface") or {}).get("name") == device_interface:
                        matches.append(ni)
    return json.dumps(matches, indent=2)
