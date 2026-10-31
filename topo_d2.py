#!/usr/bin/env python3
from mininet.net import Mininet
from mininet.node import RemoteController, OVSSwitch
from mininet.link import TCLink
from mininet.cli import CLI
from mininet.log import setLogLevel

def run():
    net = Mininet(controller=None, switch=OVSSwitch, link=TCLink, autoSetMacs=True)
    net.addController('c0', controller=RemoteController, ip='127.0.0.1', port=6653)

    h1 = net.addHost('h1', ip='10.0.0.1/24')
    h2 = net.addHost('h2', ip='10.0.0.2/24')
    s1 = net.addSwitch('s1', protocols='OpenFlow13')
    s2 = net.addSwitch('s2', protocols='OpenFlow13')
    s3 = net.addSwitch('s3', protocols='OpenFlow13')
    s4 = net.addSwitch('s4', protocols='OpenFlow13')

    net.addLink(h1, s1)
    net.addLink(h2, s4)
    # Fast path: s1 - s2 - s4   (10 ms per link)
    net.addLink(s1, s2, delay='10ms')
    net.addLink(s2, s4, delay='10ms')
    # Slow path: s1 - s3 - s4   (30 ms per link)
    net.addLink(s1, s3, delay='30ms')
    net.addLink(s3, s4, delay='30ms')

    net.start()
    CLI(net)
    net.stop()

if __name__ == '__main__':
    setLogLevel('info')
    run()
