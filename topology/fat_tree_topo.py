from mininet.topo import Topo

# Mininet derives a switch's dpid from the digits in its name, so "s0" would
# get dpid 0, which OVS rejects and replaces by a random MAC-derived dpid that
# differs on every run (all runs before this change have such a core dpid).
CORE_DPID = 1000


class FatTreeTopo(Topo):
    """
    Two-tier tree (not a k-ary fat-tree; there are no redundant paths).

    - 1 core switch (s0, dpid CORE_DPID)
    - ceil(N / 4) edge switches (s1, s2, ...; dpid = the number), each linked
      to the core
    - up to 4 hosts per edge switch (the last one may have fewer)

    Supports any N; the experiments use N = 4, 8, 16, 32, 64
    (N = 64 -> 16 edge switches, 17 switches in total).

    Selected with --topo tree; "fattree" is kept as an alias and is the name
    recorded in traffic_meta.json and summary.csv.
    """

    def build(self, n=16):
        ports_per_aggr = 4  # hosts per edge switch

        # Core switch
        core = self.addSwitch('s0', dpid=f'{CORE_DPID:016x}')

        host_id = 1
        aggr_id = 1

        while host_id <= n:
            # Add aggregation switch
            aggr = self.addSwitch(f's{aggr_id}')
            self.addLink(core, aggr)
            aggr_id += 1

            # Connect up to ports_per_aggr hosts to this aggregation switch
            for _ in range(ports_per_aggr):
                if host_id > n:
                    break
                h = self.addHost(f'h{host_id}')
                self.addLink(h, aggr)
                host_id += 1


topos = {'fattretopo': (lambda n=16: FatTreeTopo(n=n))}
