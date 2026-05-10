import asyncio
import json
import random
import httpx
from datetime import datetime, timezone, timedelta
from typing import Any


class SSRClient:
    """Async REST client for the SSR API. Handles JWT auth automatically."""

    def __init__(self, host: str, username: str, password: str, verify_ssl: bool = True, port: int = 443):
        self.base_url = f"https://{host}:{port}"
        self.username = username
        self.password = password
        self._token: str | None = None
        self._http = httpx.AsyncClient(verify=verify_ssl, base_url=self.base_url, timeout=30.0)
        self._schema_fields: dict[str, set[str]] = {}

    async def _login(self) -> None:
        response = await self._http.post(
            "/api/v1/login",
            json={"username": self.username, "password": self.password},
        )
        response.raise_for_status()
        self._token = response.json()["token"]
        await self._fetch_schema()

    async def _fetch_schema(self) -> None:
        """Fetch GraphQL schema via introspection and cache field names per type.

        Called once per login. If it fails (network error, unsupported), the cache
        remains empty and callers fall back to conservative (reduced) queries.
        """
        if self._schema_fields:
            return
        try:
            headers = {"Authorization": f"Bearer {self._token}"}
            query = "{ __schema { types { name fields { name } } } }"
            response = await self._http.post(
                "/api/v1/graphql", headers=headers, json={"query": query}
            )
            if not response.is_success:
                return
            for t in response.json().get("data", {}).get("__schema", {}).get("types", []):
                name = t.get("name")
                fields = t.get("fields") or []
                if name:
                    self._schema_fields[name] = {f["name"] for f in fields}
        except Exception:
            pass

    def _has_field(self, type_name: str, field_name: str) -> bool:
        """Return True if field_name is present on type_name in the cached schema."""
        return field_name in self._schema_fields.get(type_name, set())

    def _type_for_fields(self, *required_fields: str) -> str | None:
        """Return the name of the first GraphQL type that contains all required_fields."""
        for type_name, type_fields in self._schema_fields.items():
            if all(f in type_fields for f in required_fields):
                return type_name
        return None

    async def _get(self, path: str, params: dict | None = None) -> Any:
        if not self._token:
            await self._login()

        headers = {"Authorization": f"Bearer {self._token}"}
        response = await self._http.get(path, headers=headers, params=params)

        # Token expired — re-authenticate once and retry
        if response.status_code == 401:
            self._token = None
            await self._login()
            headers = {"Authorization": f"Bearer {self._token}"}
            response = await self._http.get(path, headers=headers, params=params)

        response.raise_for_status()
        return response.json()

    async def _post(self, path: str, body: dict) -> Any:
        if not self._token:
            await self._login()

        headers = {"Authorization": f"Bearer {self._token}"}
        response = await self._http.post(path, headers=headers, json=body)

        if response.status_code == 401:
            self._token = None
            await self._login()
            headers = {"Authorization": f"Bearer {self._token}"}
            response = await self._http.post(path, headers=headers, json=body)

        response.raise_for_status()
        return response.json()

    async def _graphql(self, query: str, variables: dict | None = None) -> Any:
        if not self._token:
            await self._login()

        headers = {"Authorization": f"Bearer {self._token}"}
        payload = {"query": query, "variables": variables or {}}
        response = await self._http.post("/api/v1/graphql", headers=headers, json=payload)

        if response.status_code == 401:
            self._token = None
            await self._login()
            headers = {"Authorization": f"Bearer {self._token}"}
            response = await self._http.post("/api/v1/graphql", headers=headers, json=payload)

        if response.is_error:
            body = response.text[:500] if response.text else "(empty)"
            raise httpx.HTTPStatusError(
                f"HTTP {response.status_code}: {body}",
                request=response.request,
                response=response,
            )
        return response.json()

    # ------------------------------------------------------------------
    # Routers
    # ------------------------------------------------------------------

    async def get_routers(self) -> list:
        return await self._get("/api/v1/router")

    async def get_connection_info(self) -> dict:
        info = await self._get("/api/v1/system")
        if info.get("isConductor"):
            mode = "conductor"
        elif info.get("isManagedByCloud"):
            mode = "router-cloud"
        elif info.get("isManaged"):
            mode = "router-managed"
        else:
            mode = "router-standalone"
        return {
            "mode": mode,
            "router": info.get("router"),
            "node": info.get("node"),
            "role": info.get("role"),
            "software_version": info.get("softwareVersion"),
            "status": info.get("status"),
            "alarm_count": info.get("alarmCount"),
            "shelved_alarm_count": info.get("shelvedAlarmCount"),
            "is_conductor": info.get("isConductor"),
            "is_managed": info.get("isManaged"),
            "is_managed_by_cloud": info.get("isManagedByCloud"),
        }

    async def get_router(self, router: str) -> dict:
        return await self._get(f"/api/v1/router/{router}")

    async def get_router_nodes(self, router: str) -> list:
        return await self._get(f"/api/v1/router/{router}/node")

    async def get_router_node(self, router: str, node: str) -> dict:
        return await self._get(f"/api/v1/router/{router}/node/{node}")

    # ------------------------------------------------------------------
    # Alarms
    # ------------------------------------------------------------------

    async def get_alarms(self, router: str | None = None, node: str | None = None) -> list:
        if router and node:
            return await self._get(f"/api/v1/router/{router}/node/{node}/alarm")
        if router:
            return await self._get(f"/api/v1/router/{router}/alarm")
        return await self._get("/api/v1/alarm")

    # ------------------------------------------------------------------
    # FIB / routing
    # ------------------------------------------------------------------

    async def get_software_version(self) -> dict:
        return await self._get("/api/software/version", params={"detail": "true"})

    async def get_app_id_modules(self, router: str, node: str) -> list:
        return await self._get(
            f"/api/v1/router/{router}/node/{node}/applicationIdModules/registration"
        )

    async def get_application_names(
        self,
        router: str,
        node: str,
        limit: int | None = None,
    ) -> list:
        page_size = 1000
        offset = 0
        entries: list = []

        while True:
            remaining = None if limit is None else limit - len(entries)
            if remaining is not None and remaining <= 0:
                break

            result = await self._get(
                f"/api/v1/router/{router}/node/{node}/applicationClassification/commonNames",
                params={
                    "elementCount": min(page_size, remaining) if remaining is not None else page_size,
                    "firstIndex": offset,
                },
            )

            # Response is expected to be a list; if shorter than page_size we're done
            page = result if isinstance(result, list) else result.get("items", [])
            entries.extend(page)

            if len(page) < page_size:
                break

            offset += len(page)

        return entries

    async def get_vrfs(self, router: str, limit: int | None = None) -> list:
        page_size = 1000
        offset = 0
        entries: list = []

        while True:
            remaining = None if limit is None else limit - len(entries)
            if remaining is not None and remaining <= 0:
                break

            result = await self._get(
                f"/api/v1/router/{router}/vrf",
                params={
                    "elementCount": min(page_size, remaining) if remaining is not None else page_size,
                    "firstIndex": offset,
                },
            )

            page = result if isinstance(result, list) else result.get("items", [])
            entries.extend(page)

            if len(page) < page_size:
                break

            offset += len(page)

        return entries

    async def get_app_id_config(self, router: str) -> dict | None:
        """Returns None when the endpoint 404s (app ID not configured on this router)."""
        try:
            return await self._get(
                f"/api/v1/config/running/authority/router/{router}/application-identification",
                params={"withDefaults": "true"},
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            raise

    async def get_web_filtering_state(self, router: str, node: str) -> dict:
        return await self._get(
            f"/api/v1/router/{router}/node/{node}/applications/state"
        )

    async def get_app_id_categories(self, router: str, node: str) -> list:
        return await self._get(
            f"/api/v1/router/{router}/node/{node}/applications/categories"
        )

    async def app_id_lookup(
        self,
        router: str,
        node: str,
        mode: str = "address",
        ip: str | None = None,
        port: int | None = None,
        protocol: str | None = None,
        domain: str | None = None,
    ) -> dict:
        params: dict = {"mode": mode}
        if mode == "address":
            params.update({"ip": ip, "port": port, "protocol": protocol})
        else:
            params["domain"] = domain
        return await self._get(
            f"/api/v1/router/{router}/node/{node}/appIdLookup",
            params=params,
        )

    async def get_session_processor_utilization(self, router: str, node: str) -> dict:
        return await self._get(
            f"/api/v1/router/{router}/node/{node}/system/utilization/sessionProc"
        )

    async def get_resource_allocation(self, router: str, node: str) -> dict:
        return await self._get(
            f"/api/v1/router/{router}/node/{node}/system/resourceAllocation"
        )

    async def fib_lookup(
        self,
        router: str,
        node: str,
        dest_ip: str,
        dest_port: int,
        protocol: str,
        tenant: str | None = None,
        source_ip: str | None = None,
        source_interface: str | None = None,
    ) -> dict:
        params: dict = {
            "destIp": dest_ip,
            "destPort": dest_port,
            "protocol": protocol,
            "detail": "true",
        }
        if tenant:
            params["tenant"] = tenant
        if source_ip:
            params["sourceIp"] = source_ip
        if source_interface:
            params["sourceInterface"] = source_interface
        return await self._get(
            f"/api/v1/router/{router}/node/{node}/traffic/fib/lookup",
            params=params,
        )

    async def get_fib(
        self,
        router: str,
        node: str,
        limit: int | None = None,
        vrf: str | None = None,
        ip_prefix: str | None = None,
    ) -> list:
        page_size = 1000
        cursor: str | None = None
        entries: list = []

        while True:
            remaining = None if limit is None else limit - len(entries)
            if remaining is not None and remaining <= 0:
                break

            params = {
                "first": min(page_size, remaining) if remaining is not None else page_size,
                "after": cursor,
                "detail": "true",
                "vrf": vrf,
                "ipPrefix": ip_prefix,
            }
            # Strip None params — SSR may reject unknown null values
            params = {k: v for k, v in params.items() if v is not None}

            result = await self._get(
                f"/api/v1/router/{router}/node/{node}/traffic/fib",
                params=params,
            )

            # SSR REST paginated responses typically return a list at the top
            # level or under a key — adjust if the actual shape differs.
            if isinstance(result, list):
                page_entries = result
                cursor = None  # no cursor in flat list response
            else:
                page_entries = result.get("nodes", result.get("items", []))
                cursor = result.get("pageInfo", {}).get("endCursor") if isinstance(result, dict) else None

            entries.extend(page_entries)

            if not cursor:
                break

        return entries

    # ------------------------------------------------------------------
    # Interfaces
    # ------------------------------------------------------------------

    _NETWORK_INTERFACE_PLUGINS_QUERY = """
    query GetNetworkInterfacePlugins(
      $routerName: String
      $nodeName: String
      $networkInterfaceName: String
    ) {
      allRouters(name: $routerName) {
        nodes {
          name
          nodes(name: $nodeName) {
            nodes {
              name
              deviceInterfaces {
                nodes {
                  networkInterfaces(name: $networkInterfaceName) {
                    nodes {
                      name
                      plugins {
                        state
                      }
                    }
                  }
                }
              }
            }
          }
        }
      }
    }
    """

    async def get_network_interface_applications(
        self,
        router: str | None = None,
        node: str | None = None,
        network_interface: str | None = None,
    ) -> dict:
        return await self._graphql(
            self._NETWORK_INTERFACE_PLUGINS_QUERY,
            {"routerName": router, "nodeName": node, "networkInterfaceName": network_interface},
        )

    _NETWORK_INTERFACES_QUERY = """
    query GetNetworkInterfaces(
      $routerName: String
      $nodeName: String
      $networkInterfaceName: String
    ) {
      allRouters(name: $routerName) {
        nodes {
          nodes(name: $nodeName) {
            nodes {
              name
              router {
                name
              }
              deviceInterfaces {
                nodes {
                  networkInterfaces(name: $networkInterfaceName) {
                    nodes {
                      name
                      addresses {
                        nodes {
                          ipAddress
                          gateway
                          prefixLength
                          pppPeerIp
                        }
                      }
                      vlan
                      type
                      dhcp
                      hostname
                      globalId
                      tunnel {
                        destination
                        source {
                          address
                        }
                      }
                      state {
                        addresses {
                          ipAddress
                          gateway
                          prefixLength
                        }
                      }
                      deviceInterface {
                        name
                        enabled
                        state {
                          operationalStatus
                          provisionalStatus
                          networkPluginState
                        }
                        type
                        forwarding
                      }
                    }
                  }
                }
              }
            }
          }
        }
      }
    }
    """

    async def get_network_interfaces(
        self,
        router: str | None = None,
        node: str | None = None,
        network_interface: str | None = None,
    ) -> dict:
        return await self._graphql(
            self._NETWORK_INTERFACES_QUERY,
            {"routerName": router, "nodeName": node, "networkInterfaceName": network_interface},
        )

    # ------------------------------------------------------------------
    # Routing / services (GraphQL)
    # ------------------------------------------------------------------

    _PEER_PATHS_QUERY = """
    query GetPeers(
      $routerName: String
      $peerName: String
      $isDetail: Boolean!
    ) {
      allRouters(name: $routerName) {
        nodes {
          peers(name: $peerName) {
            nodes {
              name
              router { name }
              paths {
                displayName
                node
                networkInterface
                adjacentAddress
                adjacentHostname
                status
                mtu
                uptime
                latency
                jitter
                loss
              }
              paths @include(if: $isDetail) {
                peerReceivedAddress
                keyExchangeAlgorithm
                dhKeySize
                mlKemKeySize
                txIntervalInEffect
                rxIntervalInEffect
                detectionMultiplier
              }
            }
          }
        }
      }
    }
    """

    # Fallback for SSR versions that don't support all detail fields (e.g. 6.3.x)
    _PEER_PATHS_QUERY_COMPAT = """
    query GetPeers(
      $routerName: String
      $peerName: String
      $isDetail: Boolean!
    ) {
      allRouters(name: $routerName) {
        nodes {
          peers(name: $peerName) {
            nodes {
              name
              router { name }
              paths {
                displayName
                node
                networkInterface
                adjacentAddress
                adjacentHostname
                status
                mtu
                uptime
                latency
                jitter
                loss
              }
              paths @include(if: $isDetail) {
                peerReceivedAddress
              }
            }
          }
        }
      }
    }
    """

    async def get_peer_paths(
        self,
        router: str | None = None,
        peer_name: str | None = None,
        detail: bool = False,
    ) -> dict:
        variables = {"routerName": router, "peerName": peer_name, "isDetail": detail}
        # keyExchangeAlgorithm was introduced alongside the other post-6.3.x detail fields;
        # its presence reliably signals that the full query is safe to use.
        query = (
            self._PEER_PATHS_QUERY
            if self._has_field("RouterPeerPathType", "keyExchangeAlgorithm")
            else self._PEER_PATHS_QUERY_COMPAT
        )
        return await self._graphql(query, variables)

    _FIND_SESSIONS_QUERY = """
    query FindSessions($filterString: String, $first: Int!) {
      allRouters {
        nodes {
          name
          nodes {
            nodes {
              name
              flowEntries(first: $first, filter: $filterString) {
                nodes {
                  sourceIp
                  destIp
                  sourcePort
                  destPort
                  vlan
                  devicePort
                  protocol
                  sessionUuid
                  natIp
                  natPort
                  serviceName
                  tenant
                  encrypted
                  inactivityTimeout
                  deviceInterfaceName
                  networkInterfaceName
                  startTime
                  forward
                }
              }
            }
          }
        }
      }
    }
    """

    async def find_sessions(
        self,
        filter: str | None = None,
        limit_per_router: int = 250,
    ) -> dict:
        result = await self._graphql(
            self._FIND_SESSIONS_QUERY,
            {"filterString": filter, "first": limit_per_router},
        )

        # Parse unreachable routers from GraphQL partial errors.
        # Error message format: "No connectivity to highwayManager@{node}.{router}"
        unreachable = []
        for error in result.get("errors", []):
            msg = error.get("message", "")
            if "highwayManager@" in msg:
                addr = msg.split("highwayManager@", 1)[1]
                dot = addr.find(".")
                if dot > 0:
                    unreachable.append({"router": addr[dot + 1:], "node": addr[:dot]})

        sessions = []
        for router_node in result.get("data", {}).get("allRouters", {}).get("nodes", []):
            router_name = router_node.get("name")
            for node in router_node.get("nodes", {}).get("nodes", []):
                node_name = node.get("name")
                for flow in (node.get("flowEntries") or {}).get("nodes", []):
                    flow["_router"] = router_name
                    flow["_node"] = node_name
                    sessions.append(flow)

        return {"sessions": sessions, "unreachable": unreachable}

    _SESSIONS_QUERY = """
    query GetSessions(
      $routerName: String!
      $nodeName: String
      $elementCount: Int!
      $startIndex: String
      $filter: String
    ) {
      allRouters(name: $routerName) {
        nodes {
          nodes(name: $nodeName) {
            nodes {
              flowEntries(
                first: $elementCount
                after: $startIndex
                filter: $filter
              ) {
                nodes {
                  sessionUuid
                  forward
                  serviceName
                  tenant
                  deviceInterfaceName
                  networkInterfaceName
                  vlan
                  protocol
                  sourceIp
                  sourcePort
                  destIp
                  destPort
                  natIp
                  natPort
                  encrypted
                  inactivityTimeout
                  startTime
                }
                pageInfo {
                  endCursor
                }
              }
            }
          }
        }
      }
    }
    """

    async def get_sessions(
        self,
        router: str,
        node: str | None = None,
        limit: int | None = None,
        filter: str | None = None,
    ) -> list:
        page_size = 1000
        cursor: str | None = None
        sessions: list = []

        while True:
            remaining = None if limit is None else limit - len(sessions)
            if remaining is not None and remaining <= 0:
                break

            result = await self._graphql(
                self._SESSIONS_QUERY,
                {
                    "routerName": router,
                    "nodeName": node,
                    "elementCount": min(page_size, remaining) if remaining is not None else page_size,
                    "filter": filter,
                    "startIndex": cursor,
                },
            )

            page = (
                result
                .get("data", {})
                .get("allRouters", {})
                .get("nodes", [{}])[0]
                .get("nodes", {})
                .get("nodes", [{}])[0]
                .get("flowEntries", {})
            )
            sessions.extend(page.get("nodes", []))
            cursor = page.get("pageInfo", {}).get("endCursor")

            if not cursor:
                break

        return sessions

    _SERVICE_PATHS_QUERY = """
    query GetServicePaths(
      $routerName: String!
      $nodeName: String
      $elementCount: Int!
      $startIndex: String
      $filter: String
    ) {
      allRouters(name: $routerName) {
        nodes {
          nodes(name: $nodeName) {
            nodes {
              servicePaths(
                first: $elementCount
                after: $startIndex
                filter: $filter
              ) {
                nodes {
                  serviceName
                  serviceRouteName
                  type
                  networkInterfaceName
                  destination
                  hostname
                  gatewayIp
                  rate
                  cost
                  vector
                  capacityUsed
                  capacityMax
                  state
                  meetsSLA
                  peerName
                  pathIndex
                  latency
                  jitter
                  loss
                  warning
                  pathWarning
                  prevMeetsSLA
                  reachabilityProbeType
                  reachabilityProbes
                }
                pageInfo {
                  endCursor
                }
              }
            }
          }
        }
      }
    }
    """

    async def get_service_paths(
        self,
        router: str,
        node: str | None = None,
        limit: int | None = None,
        filter: str | None = None,
    ) -> list:
        page_size = 1000
        cursor: str | None = None
        paths: list = []

        while True:
            remaining = None if limit is None else limit - len(paths)
            if remaining is not None and remaining <= 0:
                break

            result = await self._graphql(
                self._SERVICE_PATHS_QUERY,
                {
                    "routerName": router,
                    "nodeName": node,
                    "elementCount": min(page_size, remaining) if remaining is not None else page_size,
                    "filter": filter,
                    "startIndex": cursor,
                },
            )

            page = (
                result
                .get("data", {})
                .get("allRouters", {})
                .get("nodes", [{}])[0]
                .get("nodes", {})
                .get("nodes", [{}])[0]
                .get("servicePaths", {})
            )
            paths.extend(page.get("nodes", []))
            cursor = page.get("pageInfo", {}).get("endCursor")

            if not cursor:
                break

        return paths

    _DEVICE_INTERFACE_QUERY = """
    query GetDeviceInterfaceState(
      $routerName: String
      $metricsRouterName: String!
      $nodeName: String
      $deviceInterface: String
      $startTime: String
      $endTime: String
    ) {
      allRouters(name: $routerName) {
        nodes {
          name
          nodes(name: $nodeName) {
            nodes {
              name
              deviceInterfaces(name: $deviceInterface) {
                nodes {
                  name
                  id
                  description
                  forwarding
                  sharedPhysAddress
                  type
                  pciAddress
                  interfaceName
                  mode
                  targetInterface
                  bondInfo
                  linkSettings
                  averageBandwidth: analytic(
                    metric: BANDWIDTH
                    transform: AVERAGE
                    startTime: $startTime
                    endTime: $endTime
                  )
                  state {
                    adminStatus
                    operationalStatus
                    provisionalStatus
                    redundancyStatus
                    macAddress
                    speed
                    duplex
                    networkPluginState
                  }
                  networkInterfaces {
                    nodes {
                      name
                    }
                  }
                }
              }
            }
          }
        }
      }
      metrics {
        interface {
          received {
            bytes(router: $metricsRouterName, node: $nodeName) {
              router
              node
              device: port
              value
            }
            packets(router: $metricsRouterName, node: $nodeName) {
              router
              node
              device: port
              value
            }
            error(router: $metricsRouterName, node: $nodeName) {
              router
              node
              device: port
              value
            }
          }
          sent {
            bytes(router: $metricsRouterName, node: $nodeName) {
              router
              node
              device: port
              value
            }
            packets(router: $metricsRouterName, node: $nodeName) {
              router
              node
              device: port
              value
            }
            error(router: $metricsRouterName, node: $nodeName) {
              router
              node
              device: port
              value
            }
          }
        }
      }
    }
    """

    async def get_device_interfaces(
        self,
        router: str,
        node: str | None = None,
        device_interface: str | None = None,
        window_minutes: int = 5,
    ) -> dict:
        now = datetime.now(timezone.utc)
        start = (now - timedelta(minutes=window_minutes)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        end = now.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        return await self._graphql(
            self._DEVICE_INTERFACE_QUERY,
            {
                "routerName": router,
                "metricsRouterName": router,
                "nodeName": node,
                "deviceInterface": device_interface,
                "startTime": start,
                "endTime": end,
            },
        )

    _TOP_SOURCES_QUERY = """
    query GetTopSources(
      $routerName: String
      $nodeName: String
      $first: Int
      $orderBy: TopSourcesOrderingMetric
    ) {
      allRouters(name: $routerName) {
        nodes {
          name
          nodes(name: $nodeName) {
            nodes {
              name
              topSources(first: $first, orderBy: $orderBy) {
                tenant
                ip
                currentBandwidth
                totalData
                sessionCount
              }
            }
          }
        }
      }
    }
    """

    async def get_top_sources(
        self,
        router: str | None = None,
        node: str | None = None,
        limit: int = 10,
        order_by: str = "TOTAL_DATA",
    ) -> dict:
        return await self._graphql(
            self._TOP_SOURCES_QUERY,
            {"routerName": router, "nodeName": node, "first": limit, "orderBy": order_by},
        )

    _RIB_QUERY = """
    query GetRib(
      $routerName: String!
      $elementCount: Int!
      $startIndex: String
      $vrf: String
      $filter: String
      $ip: String
      $subCommand: RibSubCommand
    ) {
      allRouters(name: $routerName) {
        nodes {
          nodes {
            nodes {
              ribEntries(
                first: $elementCount
                after: $startIndex
                vrf: $vrf
                detailed: true
                filter: $filter
                ip: $ip
                subCommand: $subCommand
              ) {
                nodes {
                  vrf
                  prefix
                  protocol
                  selected
                  distance
                  metric
                  uptime
                  tag
                  nextHops {
                    ip
                    interfaceName
                    directlyConnected
                    recursive
                    blackhole
                    fib
                    onLink
                    source
                    reject
                    duplicate
                  }
                }
                pageInfo {
                  endCursor
                }
              }
            }
          }
        }
      }
    }
    """

    async def get_rib(
        self,
        router: str,
        vrf: str | None = None,
        ip: str | None = None,
        filter: str | None = None,
        sub_command: str | None = None,
        limit: int | None = None,
    ) -> list:
        page_size = 1000
        cursor: str | None = None
        entries: list = []

        while True:
            remaining = None if limit is None else limit - len(entries)
            if remaining is not None and remaining <= 0:
                break

            result = await self._graphql(
                self._RIB_QUERY,
                {
                    "routerName": router,
                    "elementCount": min(page_size, remaining) if remaining is not None else page_size,
                    "startIndex": cursor,
                    "vrf": vrf,
                    "filter": filter,
                    "ip": ip,
                    "subCommand": sub_command,
                },
            )

            page = (
                result
                .get("data", {})
                .get("allRouters", {})
                .get("nodes", [{}])[0]
                .get("nodes", {})
                .get("nodes", [{}])[0]
                .get("ribEntries", {})
            )
            entries.extend(page.get("nodes", []))
            cursor = page.get("pageInfo", {}).get("endCursor")

            if not cursor:
                break

        return entries

    _EVENTS_QUERY = """
    query GetEvents(
      $routerName: String!
      $from: String
      $to: String
      $type: [AuditLogType]
      $subtype: String
      $elementCount: Int
      $startIndex: String
    ) {
      allRouters(name: $routerName) {
        nodes {
          auditLogs(
            startTime: $from
            endTime: $to
            first: $elementCount
            filter: $type
            subtype: $subtype
            startIndex: $startIndex
          ) {
            nodes {
              router
              node
              type
              subtype
              timestamp
              data
            }
            pageInfo {
              endCursor
              hasNextPage
            }
          }
        }
      }
    }
    """

    async def get_events(
        self,
        router: str,
        from_time: str | None = None,
        to_time: str | None = None,
        event_types: list[str] | None = None,
        subtype: str | None = None,
        limit: int | None = None,
    ) -> list:
        page_size = 1000
        cursor: str | None = None
        events: list = []

        while True:
            remaining = None if limit is None else limit - len(events)
            if remaining is not None and remaining <= 0:
                break

            result = await self._graphql(
                self._EVENTS_QUERY,
                {
                    "routerName": router,
                    "from": from_time,
                    "to": to_time,
                    "type": event_types or [],
                    "subtype": subtype,
                    "elementCount": min(page_size, remaining) if remaining is not None else page_size,
                    "startIndex": cursor,
                },
            )

            page = (
                result
                .get("data", {})
                .get("allRouters", {})
                .get("nodes", [{}])[0]
                .get("auditLogs", {})
            )
            events.extend(page.get("nodes", []))
            cursor = page.get("pageInfo", {}).get("endCursor")

            if not cursor or not page.get("pageInfo", {}).get("hasNextPage"):
                break

        return events

    _ASSETS_QUERY = """
    query GetAssets(
      $assetIds: [String]
    ) {
      allAuthorities {
        nodes {
          assets(assetIds: $assetIds) {
            routerName
            nodeName
            assetId
            t128Version
            upgradeWarning
            status
            statusDurationSeconds
            text
            failedStatus
            firstConnectedDate
            installationType
            platformType
            errorsJson {
              env
              operation
              reason
            }
          }
          duplicateAssets(assetIds: $assetIds) {
            assetId
          }
        }
      }
    }
    """

    async def get_assets(self, asset_ids: list[str] | None = None) -> dict:
        return await self._graphql(
            self._ASSETS_QUERY,
            {"assetIds": asset_ids or []},
        )

    _SYSTEM_PROCESSES_QUERY = """
    query SystemProcesses(
      $routerName: String
      $nodeName: String
    ) {
      allRouters(name: $routerName) {
        nodes {
          name
          nodes(name: $nodeName) {
            nodes {
              name
              role
              state {
                processes {
                  name
                  status
                  primary
                  leaderStatus
                }
              }
            }
          }
        }
      }
    }
    """

    async def get_system_processes(
        self,
        router: str | None = None,
        node: str | None = None,
    ) -> dict:
        return await self._graphql(
            self._SYSTEM_PROCESSES_QUERY,
            {"routerName": router, "nodeName": node},
        )

    _APP_ID_CACHE_QUERY = """
    query GetAppIdCache(
      $routerName: String!
      $nodeName: String
      $elementCount: Int!
      $startIndex: String
      $cache: String!
    ) {
      allRouters(name: $routerName) {
        nodes {
          nodes(name: $nodeName) {
            nodes {
              name
              appIdCacheEntries(first: $elementCount, after: $startIndex, cache: $cache) {
                nodes {
                  address
                  port
                  protocol
                  domain
                  url
                  application
                  category
                  subCategory
                  timeSinceLastUsed
                }
                pageInfo {
                  endCursor
                }
              }
            }
          }
        }
      }
    }
    """

    async def get_app_id_cache(
        self,
        router: str,
        node: str | None = None,
        cache: str = "address",
        limit: int | None = None,
    ) -> list:
        page_size = 1000
        cursor: str | None = None
        entries: list = []

        while True:
            remaining = None if limit is None else limit - len(entries)
            if remaining is not None and remaining <= 0:
                break

            result = await self._graphql(
                self._APP_ID_CACHE_QUERY,
                {
                    "routerName": router,
                    "nodeName": node,
                    "cache": cache,
                    "elementCount": min(page_size, remaining) if remaining is not None else page_size,
                    "startIndex": cursor,
                },
            )

            page = (
                result
                .get("data", {})
                .get("allRouters", {})
                .get("nodes", [{}])[0]
                .get("nodes", {})
                .get("nodes", [{}])[0]
                .get("appIdCacheEntries", {})
            )
            entries.extend(page.get("nodes", []))
            cursor = page.get("pageInfo", {}).get("endCursor")

            if not cursor:
                break

        return entries

    _ARP_QUERY = """
    query getArp(
      $routerName: String!
      $nodeName: String!
      $elementCount: Int!
      $startIndex: String
    ) {
      allRouters(name: $routerName) {
        nodes {
          nodes(name: $nodeName) {
            nodes {
              arp(first: $elementCount, after: $startIndex) {
                nodes {
                  deviceInterface
                  vlan
                  ipAddress
                  destinationMac
                  state
                  timeout
                  retryCount
                  lastResolvedTimestamp
                }
                pageInfo {
                  endCursor
                }
              }
            }
          }
        }
      }
    }
    """

    async def get_arp(
        self,
        router: str,
        node: str,
        limit: int | None = None,
    ) -> list:
        page_size = 1000
        cursor: str | None = None
        entries: list = []

        while True:
            remaining = None if limit is None else limit - len(entries)
            if remaining is not None and remaining <= 0:
                break

            result = await self._graphql(
                self._ARP_QUERY,
                {
                    "routerName": router,
                    "nodeName": node,
                    "elementCount": min(page_size, remaining) if remaining is not None else page_size,
                    "startIndex": cursor,
                },
            )

            page = (
                result
                .get("data", {})
                .get("allRouters", {})
                .get("nodes", [{}])[0]
                .get("nodes", {})
                .get("nodes", [{}])[0]
                .get("arp", {})
            )
            entries.extend(page.get("nodes", []))
            cursor = page.get("pageInfo", {}).get("endCursor")

            if not cursor:
                break

        return entries

    _PLATFORM_QUERY = """
    query NodePlatform(
      $routerName: String
      $nodeName: String
    ) {
      allRouters(name: $routerName) {
        nodes {
          name
          nodes(name: $nodeName) {
            nodes {
              name
              platform {
                memory
                cpu {
                  type
                  speed
                  hyperThreading
                  cores
                  fastlaneCores
                  isolatedCoreMask
                  dedicatedCoreMask
                  powerSaver
                }
                deviceInterfaces {
                  name
                  manufacturer
                  description
                  driver
                  speed
                  pciAddress
                  macAddress
                  driverVersion
                  firmwareVersion
                  statisticsSupported
                  pluginInfo
                }
                disks {
                  name
                  space
                  percentUsed
                  powerOnHours
                  terabytesWritten
                  terabytesWrittenPerYear
                }
                operatingSystem {
                  name
                  version
                  kernelVersion
                }
                vendor {
                  vendor
                  product
                  version
                  serialNumber
                }
              }
            }
          }
        }
      }
    }
    """

    async def get_platform(
        self,
        router: str | None = None,
        node: str | None = None,
    ) -> dict:
        return await self._graphql(
            self._PLATFORM_QUERY,
            {"routerName": router, "nodeName": node},
        )

    _SYSTEM_SERVICES_QUERY = """
    query SystemServices(
      $routerName: String
      $nodeName: String
    ) {
      allRouters(name: $routerName) {
        nodes {
          name
          nodes(name: $nodeName) {
            nodes {
              name
              role
              state {
                services {
                  name
                  activeState
                }
              }
            }
          }
        }
      }
    }
    """

    async def get_system_services(
        self,
        router: str | None = None,
        node: str | None = None,
    ) -> dict:
        return await self._graphql(
            self._SYSTEM_SERVICES_QUERY,
            {"routerName": router, "nodeName": node},
        )

    _SYSTEM_CONNECTIVITY_QUERY = """
    query SystemConnectivity(
      $routerName: String
      $nodeName: String
    ) {
      allRouters(name: $routerName) {
        nodes {
          name
          nodes(name: $nodeName) {
            nodes {
              name
              role
              connectivity {
                remoteNodeName
                remoteRouterName
                status
              }
            }
          }
        }
      }
    }
    """

    async def get_system_connectivity(
        self,
        router: str | None = None,
        node: str | None = None,
    ) -> dict:
        return await self._graphql(
            self._SYSTEM_CONNECTIVITY_QUERY,
            {"routerName": router, "nodeName": node},
        )

    _SYSTEM_STATE_QUERY = """
    query SystemState(
      $routerName: String
      $nodeName: String
    ) {
      allRouters(name: $routerName) {
        nodes {
          name
          nodes(name: $nodeName) {
            nodes {
              name
              state {
                status
                startTime
                role
                softwareVersion
                alarmCount
              }
            }
          }
        }
      }
    }
    """

    async def get_system_state(
        self,
        router: str | None = None,
        node: str | None = None,
    ) -> dict:
        return await self._graphql(
            self._SYSTEM_STATE_QUERY,
            {"routerName": router, "nodeName": node},
        )

    _CAPACITY_QUERY = """
    query GetNetworkCapacity(
      $routerName: String
      $nodeName: String
    ) {
      allRouters(name: $routerName) {
        nodes {
          name
          nodes(name: $nodeName) {
            nodes {
              name
              role
              networkResources {
                name
                count
                limit
              }
            }
          }
        }
      }
    }
    """

    async def get_capacity(
        self,
        router: str | None = None,
        node: str | None = None,
    ) -> dict:
        return await self._graphql(
            self._CAPACITY_QUERY,
            {"routerName": router, "nodeName": node},
        )

    _TENANT_MEMBERS_QUERY = """
    query TenantMembers(
      $routerName: String!
      $nodeName: String!
      $elementCount: Int!
      $startIndex: String
    ) {
      allRouters(name: $routerName) {
        nodes {
          nodes(name: $nodeName) {
            nodes {
              sourceTenantEntries(first: $elementCount, after: $startIndex) {
                nodes {
                  devicePortName
                  vlan
                  networkInterfaceName
                  localInterfaceIp
                  sourceIpPrefix
                  tenantName
                  sourceType
                }
                pageInfo {
                  hasNextPage
                  endCursor
                }
              }
            }
          }
        }
      }
    }
    """

    async def get_tenant_members(
        self,
        router: str,
        node: str,
        limit: int | None = None,
    ) -> list:
        page_size = 1000
        cursor: str | None = None
        entries: list = []

        while True:
            remaining = None if limit is None else limit - len(entries)
            if remaining is not None and remaining <= 0:
                break

            result = await self._graphql(
                self._TENANT_MEMBERS_QUERY,
                {
                    "routerName": router,
                    "nodeName": node,
                    "elementCount": min(page_size, remaining) if remaining is not None else page_size,
                    "startIndex": cursor,
                },
            )

            page = (
                result
                .get("data", {})
                .get("allRouters", {})
                .get("nodes", [{}])[0]
                .get("nodes", {})
                .get("nodes", [{}])[0]
                .get("sourceTenantEntries", {})
            )
            entries.extend(page.get("nodes", []))
            cursor = page.get("pageInfo", {}).get("endCursor")

            if not cursor:
                break

        return entries

    _SERVICES_QUERY = """
    query getServiceInfo(
      $routerName: String
      $nodeName: String
      $filter: String
    ) {
      allRouters(name: $routerName) {
        nodes {
          name
          nodes(name: $nodeName) {
            nodes {
              name
              serviceInfo(filter: $filter) {
                serviceName
                servicePaths {
                  upPaths
                  downPaths
                }
                routeType
                serviceRoutes {
                  name
                  nextHopType
                  policy
                }
                servicePolicy
                access {
                  allowed
                  denied
                }
                prefixes
                transport {
                  protocol
                  portRange {
                    startPort
                    endPort
                  }
                }
                enabled
                type
                autoGenerated
              }
            }
          }
        }
      }
      metrics {
        aggregateSession {
          service {
            sessionCount(router: $routerName) {
              service
              value
            }
            bandwidthTransmitted(router: $routerName) {
              service
              value
            }
            bandwidthReceived(router: $routerName) {
              service
              value
            }
          }
        }
      }
    }
    """

    async def get_services(
        self,
        router: str,
        node: str | None = None,
        filter: str | None = None,
    ) -> dict:
        return await self._graphql(
            self._SERVICES_QUERY,
            {"routerName": router, "nodeName": node, "filter": filter},
        )

    _SESSION_DETAIL_QUERY = """
    query GetSessionInfo(
      $routerName: String
      $nodeName: String
      $sessionId: String!
    ) {
      allRouters(name: $routerName) {
        nodes {
          name
          nodes(name: $nodeName) {
            nodes {
              name
              session(sessionId: $sessionId) {
                sessionId
                sessionType
                serviceName
                serviceRouteName
                sessionSource
                serviceClass
                sourceTenant
                payloadSecurityPolicy
                payloadEncrypted
                ingressSourceNat
                interNode
                interRouter
                peerName
                commonNameInfo
                appIdentification {
                  application
                  domainName
                  uri
                  category
                  subcategory
                  overrideServiceName
                  appStatsTrackingKey
                }
                tcpTimeToEstablish
                tlsTimeToEstablish
                keyInfo {
                  forwardSessionKey
                  reverseSessionKey
                }
                stateInfo {
                  sessionState
                  redundancyState
                }
                timeInfo {
                  startTime
                  ttlDurationForDatabase
                }
                forwardFlows {
                  key
                  direction
                  tcpState
                  packetsReceived
                  packetsSent
                  bytesReceived
                  bytesSent
                  tcpRetransmissionCount
                  decryptSecurityPolicy
                  actionList
                  timeToLive
                  pathIndex
                  pathAttributes {
                    pathKey
                    arpStatus
                    waypointKey
                    sourceNatKey
                    metadataSecurityPolicy
                  }
                }
                reverseFlows {
                  key
                  direction
                  tcpState
                  packetsReceived
                  packetsSent
                  bytesReceived
                  bytesSent
                  tcpRetransmissionCount
                  decryptSecurityPolicy
                  actionList
                  timeToLive
                  pathIndex
                  pathAttributes {
                    pathKey
                    arpStatus
                    waypointKey
                    sourceNatKey
                    metadataSecurityPolicy
                  }
                }
                detachedFlows {
                  key
                  direction
                  tcpState
                  packetsReceived
                  packetsSent
                  bytesReceived
                  bytesSent
                  tcpRetransmissionCount
                  decryptSecurityPolicy
                  actionList
                  timeToLive
                  pathIndex
                  pathAttributes {
                    pathKey
                    arpStatus
                    waypointKey
                    sourceNatKey
                    metadataSecurityPolicy
                  }
                }
              }
            }
          }
        }
      }
    }
    """

    _SESSION_DETAIL_QUERY_WITH_SOURCE_PEER = _SESSION_DETAIL_QUERY.replace(
        "                peerName\n                commonNameInfo",
        "                peerName\n                sourcePeerName\n                commonNameInfo",
    )

    async def get_session(
        self,
        session_id: str,
        router: str,
        node: str | None = None,
    ) -> dict:
        variables = {"sessionId": session_id, "routerName": router, "nodeName": node}
        # sourcePeerName was added in a later SSR release; find the session type
        # dynamically so the check doesn't depend on knowing the exact type name.
        session_type = self._type_for_fields("sessionId", "peerName", "serviceName")
        query = (
            self._SESSION_DETAIL_QUERY_WITH_SOURCE_PEER
            if session_type and self._has_field(session_type, "sourcePeerName")
            else self._SESSION_DETAIL_QUERY
        )
        return await self._graphql(query, variables)

    # ------------------------------------------------------------------
    # Config
    # ------------------------------------------------------------------

    async def get_running_config(self, router: str | None = None) -> dict:
        if router:
            return await self._get(f"/api/v1/config/running/authority/router/{router}")
        return await self._get("/api/v1/config/running")

    # ------------------------------------------------------------------
    # Dropped packets stream
    # ------------------------------------------------------------------

    async def get_dropped_packets(
        self,
        router: str,
        node: str,
        duration: float = 10.0,
        filter_body: dict | None = None,
    ) -> dict:
        if not self._token:
            await self._login()

        url = f"/api/v1/router/{router}/node/{node}/traffic/droppedPackets"
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
            "Accept-Encoding": "gzip, deflate, br, zstd",
        }

        events: list = []
        skipped_count: int = 0

        async def _collect() -> None:
            nonlocal skipped_count
            stream_kwargs = {"headers": headers, "timeout": None}
            if filter_body:
                stream_kwargs["json"] = filter_body
            async with self._http.stream("POST", url, **stream_kwargs) as response:
                if response.status_code == 401:
                    response.read()  # drain before raising
                    raise httpx.HTTPStatusError("Unauthorized", request=response.request, response=response)
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        events.extend(data.get("events", []))
                        skipped_count += len(data.get("skipped", []))
                    except json.JSONDecodeError:
                        pass

        try:
            await asyncio.wait_for(_collect(), timeout=duration)
        except asyncio.TimeoutError:
            pass

        return {"events": events, "skipped_count": skipped_count}

    # ------------------------------------------------------------------
    # Node utilization
    # ------------------------------------------------------------------

    _NODE_UTILIZATION_QUERY = """
    query GetNodeUtilization($routerName: String!, $nodeName: String) {
      allRouters(name: $routerName) {
        nodes {
          name
          nodes(name: $nodeName) {
            nodes {
              name
              cpu {
                core
                utilization
                type
              }
              memory {
                capacity
                usage
              }
              disk {
                capacity
                usage
                partition
              }
            }
          }
        }
      }
    }
    """

    async def get_node_utilization(
        self,
        router: str,
        node: str | None = None,
    ) -> dict:
        return await self._graphql(
            self._NODE_UTILIZATION_QUERY,
            {"routerName": router, "nodeName": node},
        )

    # ------------------------------------------------------------------
    # Applications series
    # ------------------------------------------------------------------

    async def get_application_series(
        self,
        router: str,
        node: str,
        window_minutes: int = 30,
    ) -> list:
        now = datetime.now(timezone.utc)
        start = (now - timedelta(minutes=window_minutes)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        end = now.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        body = {
            "window": {"start": start, "end": end},
            "expand": ["address"],
        }
        return await self._post(
            f"/api/v1/router/{router}/node/{node}/applications/series",
            body,
        )

    # ------------------------------------------------------------------
    # Metrics
    # ------------------------------------------------------------------

    async def query_stats(
        self,
        router: str,
        stat_id: str,
        parameters: list[dict] | None = None,
    ) -> list:
        body = {"parameters": parameters or []}
        return await self._post(f"/api/v1/router/{router}{stat_id}", body)

    async def query_metrics(
        self,
        router: str,
        metric_id: str,
        window_seconds: int = 1800,
        transform: str = "average",
        resolution: int = 2,
        filters: dict | None = None,
    ) -> list:
        body = {
            "id": metric_id,
            "transform": transform,
            "resolution": resolution,
            "window": {"start": f"now-{window_seconds}", "end": "now"},
            "order": "descending",
            "filters": filters or {},
        }
        return await self._post(f"/api/v1/router/{router}/metrics", body)

    # ------------------------------------------------------------------
    # BGP
    # ------------------------------------------------------------------

    async def get_bgp_summary(
        self,
        router: str,
        vrf: str = "default",
        address_family: str = "all",
    ) -> dict:
        params = {"addressFamily": address_family, "vrf": vrf}
        return await self._get(f"/api/v1/router/{router}/routing/bgp/summary", params=params)

    async def get_bgp_routes(
        self,
        router: str,
        vrf: str = "default",
        address_family: str = "ipv4",
    ) -> dict:
        params = {"addressFamily": address_family, "vrf": vrf}
        return await self._get(f"/api/v1/router/{router}/routing/bgp", params=params)

    async def get_bgp_advertised_routes(
        self,
        router: str,
        neighbor: str,
        vrf: str = "default",
        address_family: str = "ipv4",
    ) -> dict:
        params = {"addressFamily": address_family, "neighborAddress": neighbor, "vrf": vrf}
        return await self._get(
            f"/api/v1/router/{router}/routing/bgp/neighbors/advertised-routes", params=params
        )

    async def get_bgp_received_routes(
        self,
        router: str,
        neighbor: str,
        vrf: str = "default",
        address_family: str = "ipv4",
    ) -> dict:
        params = {"addressFamily": address_family, "neighborAddress": neighbor, "vrf": vrf}
        return await self._get(
            f"/api/v1/router/{router}/routing/bgp/neighbors/received-routes", params=params
        )

    async def get_bgp_neighbors(
        self,
        router: str,
        vrf: str = "default",
        address_family: str = "ipv4",
        neighbor: str | None = None,
    ) -> dict:
        params: dict = {"addressFamily": address_family, "vrf": vrf}
        if neighbor:
            params["neighborAddress"] = neighbor

        neighbors_data, services_data = await asyncio.gather(
            self._get(f"/api/v1/router/{router}/routing/bgp/neighbors", params=params),
            self.get_services(router, filter='"service_name"~"_bgp_"'),
        )

        svr_ips: set[str] = set()
        for rtr_node in (services_data.get("data", {}).get("allRouters", {}).get("nodes") or []):
            for node in (rtr_node.get("nodes") or {}).get("nodes") or []:
                for svc in node.get("serviceInfo") or []:
                    if not svc.get("serviceName", "").startswith("_bgp_"):
                        continue
                    for prefix in svc.get("prefixes") or []:
                        if isinstance(prefix, str) and prefix.endswith("/32"):
                            svr_ips.add(prefix[:-3])

        result = dict(neighbors_data) if isinstance(neighbors_data, dict) else {"data": neighbors_data}
        result["_svr_neighbors"] = sorted(svr_ips)
        return result

    # ------------------------------------------------------------------
    # IDP
    # ------------------------------------------------------------------

    async def get_source_nat_utilization(self, router: str, node: str) -> dict:
        return {
            "available": False,
            "reason": "Source NAT pool utilization is not yet exposed via the SSR REST or GraphQL APIs.",
            "router": router,
            "node": node,
        }

    async def get_waypoint_utilization(self, router: str, node: str) -> dict:
        return {
            "available": False,
            "reason": "Waypoint pool utilization is not yet exposed via the SSR REST or GraphQL APIs.",
            "router": router,
            "node": node,
        }

    async def get_idp_status(self, router: str, node: str) -> dict:
        engine_data = await self._get(f"/api/v1/router/{router}/node/{node}/cadillac/state")

        if engine_data.get("idpTopology") == "disabled":
            pod_data = await self._get(f"/api/v1/router/{router}/node/{node}/pods/csrx")
            return {"engine": engine_data, "pod": pod_data}

        pod_data, monitoring_data, idp_data = await asyncio.gather(
            self._get(f"/api/v1/router/{router}/node/{node}/pods/csrx"),
            self._get(f"/api/v1/router/{router}/node/{node}/cadillac/state/monitoring"),
            self._get(f"/api/v1/router/{router}/node/{node}/cadillac/state/idp"),
            return_exceptions=True,
        )
        result: dict = {"engine": engine_data}
        result["pod"] = pod_data if not isinstance(pod_data, Exception) else {"error": str(pod_data)}
        result["monitoring"] = monitoring_data if not isinstance(monitoring_data, Exception) else {"error": str(monitoring_data)}
        result["idp"] = idp_data if not isinstance(idp_data, Exception) else {"error": str(idp_data)}
        return result

    # ------------------------------------------------------------------
    # Ping
    # ------------------------------------------------------------------

    _PING_QUERY = """
    query Ping(
      $routerName: String!,
      $nodeName: String!,
      $destinationIp: String!,
      $size: Int!,
      $timeout: Int!,
      $dontFragBit: Boolean,
      $gatewayIp: String,
      $identifier: Int!,
      $sequence: Int!,
      $egressInterface: String
    ) {
      ping(
        routerName: $routerName
        nodeName: $nodeName
        identifier: $identifier
        sequence: $sequence
        destinationIp: $destinationIp
        size: $size
        timeout: $timeout
        dontFragBit: $dontFragBit
        gatewayIp: $gatewayIp
        egressInterface: $egressInterface
      ) {
        status
        statusReason
        reachable
        sequence
        ttl
        responseTime
      }
    }
    """

    async def ping(
        self,
        router: str,
        node: str,
        destination_ip: str,
        count: int = 10,
        size: int = 56,
        timeout: int = 3,
        egress_interface: str | None = None,
        dont_frag: bool = False,
    ) -> dict:
        identifier = random.randint(1, 65535)
        results = []

        for seq in range(count):
            variables = {
                "routerName": router,
                "nodeName": node,
                "destinationIp": destination_ip,
                "size": size,
                "timeout": timeout,
                "dontFragBit": dont_frag,
                "gatewayIp": "",
                "identifier": identifier,
                "sequence": seq,
                "egressInterface": egress_interface or "",
            }

            if not self._token:
                await self._login()

            headers = {"Authorization": f"Bearer {self._token}"}
            payload = {"query": self._PING_QUERY, "variables": variables}
            response = await self._http.post(
                "/api/v1/graphql", headers=headers, json=payload,
                timeout=timeout + 2,
            )

            if response.status_code == 401:
                self._token = None
                await self._login()
                headers = {"Authorization": f"Bearer {self._token}"}
                response = await self._http.post(
                    "/api/v1/graphql", headers=headers, json=payload,
                    timeout=timeout + 2,
                )

            response.raise_for_status()
            data = response.json()
            ping_result = data.get("data", {}).get("ping", {})
            results.append(ping_result)

        sent = count
        received = [r for r in results if r.get("reachable")]
        loss_pct = round((sent - len(received)) / sent * 100, 1)

        rtts = []
        for r in received:
            try:
                rtts.append(float(r["responseTime"]))
            except (KeyError, ValueError, TypeError):
                pass

        summary: dict = {
            "destination": destination_ip,
            "router": router,
            "node": node,
            "egress_interface": egress_interface,
            "packets_sent": sent,
            "packets_received": len(received),
            "packet_loss_pct": loss_pct,
        }

        if rtts:
            avg = sum(rtts) / len(rtts)
            jitter = (sum((r - avg) ** 2 for r in rtts) / len(rtts)) ** 0.5
            summary["rtt_min_ms"] = round(min(rtts), 3)
            summary["rtt_avg_ms"] = round(avg, 3)
            summary["rtt_max_ms"] = round(max(rtts), 3)
            summary["rtt_jitter_ms"] = round(jitter, 3)

        summary["probes"] = [
            {
                "seq": r.get("sequence"),
                "reachable": r.get("reachable"),
                "rtt_ms": r.get("responseTime"),
                "ttl": r.get("ttl"),
            }
            for r in results
        ]

        return summary

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def close(self) -> None:
        await self._http.aclose()
