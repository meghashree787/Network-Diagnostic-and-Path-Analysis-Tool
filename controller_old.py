import json
import time

import networkx as nx
from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet, ethernet, arp, ipv4, ether_types
from ryu.topology import event, api as topo_api
from ryu.app.wsgi import ControllerBase, WSGIApplication, route
from webob import Response

# Configured link delays (ms), used as path weights
LINK_COST = {(1, 2): 10, (2, 4): 10, (1, 3): 30, (3, 4): 30}
COOKIE = 0x1


class DiagController(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]
    _CONTEXTS = {'wsgi': WSGIApplication}

    def __init__(self, *args, **kwargs):
        super(DiagController, self).__init__(*args, **kwargs)
        self.dps = {}          # dpid -> datapath
        self.link_ports = {}   # (src_dpid, dst_dpid) -> src port number
        self.hosts = {}        # mac -> {dpid, port, ip}
        self.avoid = set()     # switches excluded from routing (policy)
        self.event_log = []       # event log for the analyzer
        kwargs['wsgi'].register(DiagRest, {'app': self})

    def log_event(self, text):
        self.event_log.append({"time": round(time.time(), 3), "event": text})
        self.logger.info("EVENT: %s", text)

    # ---------- switch and topology events ----------
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def features_handler(self, ev):
        dp = ev.msg.datapath
        self.dps[dp.id] = dp
        ofp, parser = dp.ofproto, dp.ofproto_parser
        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofp.OFPP_CONTROLLER, ofp.OFPCML_NO_BUFFER)]
        self.add_flow(dp, 0, match, actions, cookie=0)
        self.log_event("switch s%d connected" % dp.id)

    @set_ev_cls(event.EventSwitchLeave)
    def switch_leave(self, ev):
        dpid = ev.switch.dp.id
        self.dps.pop(dpid, None)
        for k in [k for k in self.link_ports if dpid in k]:
            self.link_ports.pop(k)
        self.log_event("switch s%d left" % dpid)
        self.flush_flows()

    @set_ev_cls(event.EventLinkAdd)
    def link_add(self, ev):
        s, d = ev.link.src.dpid, ev.link.dst.dpid
        self.link_ports[(s, d)] = ev.link.src.port_no
        if s < d:
            self.log_event("link UP s%d-s%d" % (s, d))
        self.flush_flows()

    @set_ev_cls(event.EventLinkDelete)
    def link_delete(self, ev):
        s, d = ev.link.src.dpid, ev.link.dst.dpid
        self.link_ports.pop((s, d), None)
        if s < d:
            self.log_event("link DOWN s%d-s%d" % (s, d))
        self.flush_flows()

    # ---------- flow helpers ----------
    def add_flow(self, dp, priority, match, actions, idle=0, cookie=0):
        ofp, parser = dp.ofproto, dp.ofproto_parser
        inst = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
        dp.send_msg(parser.OFPFlowMod(datapath=dp, priority=priority, match=match,
                                      instructions=inst, idle_timeout=idle,
                                      cookie=cookie))

    def flush_flows(self):
        """Delete all path flows (cookie=COOKIE) so traffic is re-routed."""
        for dp in list(self.dps.values()):
            ofp, parser = dp.ofproto, dp.ofproto_parser
            dp.send_msg(parser.OFPFlowMod(
                datapath=dp, command=ofp.OFPFC_DELETE,
                out_port=ofp.OFPP_ANY, out_group=ofp.OFPG_ANY,
                cookie=COOKIE, cookie_mask=0xFFFFFFFFFFFFFFFF,
                match=parser.OFPMatch()))

    def switch_link_ports(self, dpid):
        return set(p for (s, d), p in self.link_ports.items() if s == dpid)

    # ---------- packet handling ----------
    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        msg = ev.msg
        dp = msg.datapath
        dpid = dp.id
        in_port = msg.match['in_port']
        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)
        if eth is None or eth.ethertype in (ether_types.ETH_TYPE_LLDP,
                                            ether_types.ETH_TYPE_IPV6):
            return
        src, dst = eth.src, eth.dst

        if in_port not in self.switch_link_ports(dpid):
            self.learn_host(src, dpid, in_port, pkt)

        is_multicast = int(dst.split(':')[0], 16) & 1
        if is_multicast or dst not in self.hosts:
            self.flood_edge_ports(msg, dpid, in_port)
        else:
            self.route_packet(msg, dpid, in_port, dst)

    def learn_host(self, mac, dpid, port, pkt):
        a = pkt.get_protocol(arp.arp)
        ip4 = pkt.get_protocol(ipv4.ipv4)
        ip = a.src_ip if a else (ip4.src if ip4 else None)
        h = self.hosts.setdefault(mac, {})
        h['dpid'] = dpid
        h['port'] = port
        if ip and ip != '0.0.0.0':
            h['ip'] = ip

    def flood_edge_ports(self, msg, origin_dpid, in_port):
        """Flood only out of host-facing ports (never inter-switch links),
        so broadcasts cannot loop in a topology with alternative paths."""
        for dpid, dp in list(self.dps.items()):
            ofp, parser = dp.ofproto, dp.ofproto_parser
            sw = topo_api.get_switch(self, dpid)
            if not sw:
                continue
            link_p = self.switch_link_ports(dpid)
            for port in sw[0].ports:
                pn = port.port_no
                if pn in link_p or pn >= ofp.OFPP_MAX:
                    continue
                if dpid == origin_dpid and pn == in_port:
                    continue
                dp.send_msg(parser.OFPPacketOut(
                    datapath=dp, buffer_id=ofp.OFP_NO_BUFFER,
                    in_port=ofp.OFPP_CONTROLLER,
                    actions=[parser.OFPActionOutput(pn)], data=msg.data))

    def route_packet(self, msg, dpid, in_port, dst_mac):
        h = self.hosts[dst_mac]
        path = self.compute_path(dpid, h['dpid'])
        if not path:
            self.logger.info("no path s%d -> s%d, dropping", dpid, h['dpid'])
            return
        out_ports = []
        for i, sw in enumerate(path):
            out = h['port'] if i == len(path) - 1 else self.link_ports[(sw, path[i + 1])]
            dp = self.dps.get(sw)
            if dp is not None:
                match = dp.ofproto_parser.OFPMatch(eth_dst=dst_mac)
                self.add_flow(dp, 10, match,
                              [dp.ofproto_parser.OFPActionOutput(out)],
                              idle=30, cookie=COOKIE)
            out_ports.append(out)
        dp = self.dps[dpid]
        ofp, parser = dp.ofproto, dp.ofproto_parser
        dp.send_msg(parser.OFPPacketOut(
            datapath=dp, buffer_id=ofp.OFP_NO_BUFFER, in_port=in_port,
            actions=[parser.OFPActionOutput(out_ports[0])], data=msg.data))

    # ---------- path computation ----------
    def build_graph(self):
        g = nx.Graph()
        for (s, d) in self.link_ports:
            if s < d and (d, s) in self.link_ports:
                g.add_edge(s, d, weight=LINK_COST.get((s, d), 10))
        return g

    def compute_path(self, src, dst):
        if src == dst:
            return [src]
        g = self.build_graph()
        g.remove_nodes_from([n for n in self.avoid if n not in (src, dst)])
        try:
            return nx.shortest_path(g, src, dst, weight='weight')
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            return None

    # ---------- data for the REST API ----------
    def get_state(self):
        links = [{"a": "s%d" % s, "b": "s%d" % d, "cost_ms": LINK_COST.get((s, d), 10)}
                 for (s, d) in self.link_ports
                 if s < d and (d, s) in self.link_ports]
        hosts = {mac: {"ip": h.get("ip"), "switch": "s%d" % h["dpid"], "port": h["port"]}
                 for mac, h in self.hosts.items()}
        return {"switches": sorted("s%d" % d for d in self.dps),
                "links": links, "hosts": hosts,
                "avoid": sorted("s%d" % n for n in self.avoid),
                "events": self.event_log[-100:]}

    def path_between(self, src_ip, dst_ip):
        def find(ip):
            for h in self.hosts.values():
                if h.get('ip') == ip:
                    return h
            return None
        a, b = find(src_ip), find(dst_ip)
        if not a or not b:
            return {"error": "host not known yet - run pingall", "path": None}
        path = self.compute_path(a['dpid'], b['dpid'])
        if not path:
            return {"src": src_ip, "dst": dst_ip, "path": None, "cost_ms": None}
        cost = sum(LINK_COST.get(tuple(sorted((x, y))), 10)
                   for x, y in zip(path, path[1:]))
        return {"src": src_ip, "dst": dst_ip,
                "path": ["s%d" % n for n in path], "cost_ms": cost}

    def set_avoid(self, switches):
        self.avoid = set(int(x) for x in switches)
        self.log_event("policy: avoid switches %s" % sorted(self.avoid))
        self.flush_flows()


class DiagRest(ControllerBase):
    def __init__(self, req, link, data, **config):
        super(DiagRest, self).__init__(req, link, data, **config)
        self.app = data['app']

    def _json(self, obj, status=200):
        return Response(content_type='application/json', status=status,
                        body=json.dumps(obj).encode('utf-8'))

    @route('diag_state', '/diag/state', methods=['GET'])
    def state(self, req, **kwargs):
        return self._json(self.app.get_state())

    @route('diag_path', '/diag/path', methods=['GET'])
    def path(self, req, **kwargs):
        return self._json(self.app.path_between(req.GET.get('src'), req.GET.get('dst')))

    @route('diag_policy', '/diag/policy', methods=['POST'])
    def policy(self, req, **kwargs):
        try:
            body = json.loads(req.body.decode('utf-8'))
            self.app.set_avoid(body.get('avoid', []))
        except Exception as e:
            return self._json({"error": str(e)}, status=400)
        return self._json({"ok": True, "avoid": sorted(self.app.avoid)})
