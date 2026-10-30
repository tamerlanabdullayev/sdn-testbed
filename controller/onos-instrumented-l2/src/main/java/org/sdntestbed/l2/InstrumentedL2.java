package org.sdntestbed.l2;

import org.onlab.packet.Ethernet;
import org.onlab.packet.MacAddress;
import org.onosproject.core.ApplicationId;
import org.onosproject.core.CoreService;
import org.onosproject.net.Device;
import org.onosproject.net.DeviceId;
import org.onosproject.net.PortNumber;
import org.onosproject.net.device.DeviceEvent;
import org.onosproject.net.device.DeviceListener;
import org.onosproject.net.device.DeviceService;
import org.onosproject.net.flow.DefaultFlowRule;
import org.onosproject.net.flow.DefaultTrafficSelector;
import org.onosproject.net.flow.DefaultTrafficTreatment;
import org.onosproject.net.flow.FlowRule;
import org.onosproject.net.flow.FlowRuleService;
import org.onosproject.net.packet.InboundPacket;
import org.onosproject.net.packet.PacketContext;
import org.onosproject.net.packet.PacketProcessor;
import org.onosproject.net.packet.PacketService;
import org.osgi.service.component.annotations.Activate;
import org.osgi.service.component.annotations.Component;
import org.osgi.service.component.annotations.Deactivate;
import org.osgi.service.component.annotations.Reference;
import org.osgi.service.component.annotations.ReferenceCardinality;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.BufferedWriter;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicLong;

/**
 * ONOS port of controller/ryu_instrumented_switch.py; the contract is
 * docs/ADAPTER.md Part 1.
 *
 * Table-miss (priority 0, empty match, OUTPUT:CONTROLLER) on device add;
 * on Packet-In: learn src, then either a permanent priority-1 FlowRule on
 * (in_port, eth_src, eth_dst) plus a PacketOut to the learned port, or a
 * flooding PacketOut.
 *
 * proc_time_ms: process() entry to after the last send call returned
 * (FlowRuleService.applyFlowRules and PacketContext.send are both
 * asynchronous: they queue the message, like Ryu's send_msg).
 * Unlike Ryu, ONOS calls process() from several OpenFlow I/O threads.
 */
@Component(immediate = true)
public class InstrumentedL2 {

    private static final String APP_NAME = "org.sdntestbed.instrumentedl2";
    private static final int FLOW_PRIORITY = 1;
    private static final int TABLE_MISS_PRIORITY = 0;

    private final Logger log = LoggerFactory.getLogger(getClass());

    @Reference(cardinality = ReferenceCardinality.MANDATORY)
    protected CoreService coreService;

    @Reference(cardinality = ReferenceCardinality.MANDATORY)
    protected PacketService packetService;

    @Reference(cardinality = ReferenceCardinality.MANDATORY)
    protected FlowRuleService flowRuleService;

    @Reference(cardinality = ReferenceCardinality.MANDATORY)
    protected DeviceService deviceService;

    private ApplicationId appId;
    private final PacketProcessor processor = new L2Processor();
    private final DeviceListener deviceListener = new TableMissInstaller();
    private final Map<DeviceId, Map<MacAddress, PortNumber>> macToPort = new ConcurrentHashMap<>();

    private final AtomicLong packetInCount = new AtomicLong();
    private final AtomicLong flowModCount = new AtomicLong();

    private BufferedWriter flowEvents;
    private BufferedWriter ctrlMetrics;
    private ScheduledExecutorService ticker;

    @Activate
    protected void activate() {
        appId = coreService.registerApplication(APP_NAME);
        String outdir = System.getenv().getOrDefault("OUTDIR", "results/raw/devrun");
        try {
            Path dir = Paths.get(outdir);
            Files.createDirectories(dir);
            flowEvents = Files.newBufferedWriter(dir.resolve("flow_events.csv"), StandardCharsets.UTF_8);
            flowEvents.write("ts_wall,dpid,src_mac,dst_mac,in_port,proc_time_ms,msg_len\n");
            flowEvents.flush();
            ctrlMetrics = Files.newBufferedWriter(dir.resolve("controller_metrics.csv"), StandardCharsets.UTF_8);
            ctrlMetrics.write("ts_wall,packet_in_count,flow_mod_count\n");
            ctrlMetrics.flush();
        } catch (IOException e) {
            log.error("cannot open output files in {}", outdir, e);
        }

        ticker = Executors.newSingleThreadScheduledExecutor();
        ticker.scheduleAtFixedRate(this::writeMetricsRow, 0, 1, TimeUnit.SECONDS);

        packetService.addProcessor(processor, PacketProcessor.director(1));
        deviceService.addListener(deviceListener);
        for (Device d : deviceService.getAvailableDevices()) {
            installTableMiss(d.id());
        }
        log.info("Started {} (OUTDIR={})", APP_NAME, outdir);
    }

    @Deactivate
    protected void deactivate() {
        deviceService.removeListener(deviceListener);
        packetService.removeProcessor(processor);
        flowRuleService.removeFlowRulesById(appId);
        ticker.shutdownNow();
        writeMetricsRow();
        closeQuietly(flowEvents);
        closeQuietly(ctrlMetrics);
        log.info("Stopped {}", APP_NAME);
    }

    private void installTableMiss(DeviceId dev) {
        FlowRule rule = DefaultFlowRule.builder()
                .forDevice(dev)
                .withSelector(DefaultTrafficSelector.emptySelector())
                .withTreatment(DefaultTrafficTreatment.builder().punt().build())
                .withPriority(TABLE_MISS_PRIORITY)
                .fromApp(appId)
                .makePermanent()
                .build();
        flowRuleService.applyFlowRules(rule);   // not counted (as in the Ryu app)
    }

    private void writeMetricsRow() {
        long pi = packetInCount.getAndSet(0);
        long fm = flowModCount.getAndSet(0);
        writeLine(ctrlMetrics, wallTime() + "," + pi + "," + fm + "\n");
    }

    private static String wallTime() {
        return String.format("%.6f", System.currentTimeMillis() / 1000.0);
    }

    private static long dpid(DeviceId id) {
        // "of:0000000000000001" -> 1
        return Long.parseUnsignedLong(id.uri().getSchemeSpecificPart(), 16);
    }

    private static String mac(MacAddress m) {
        return m.toString().toLowerCase();
    }

    private void writeLine(BufferedWriter w, String line) {
        if (w == null) {
            return;
        }
        synchronized (w) {
            try {
                w.write(line);
                w.flush();
            } catch (IOException e) {
                log.warn("write failed", e);
            }
        }
    }

    private static void closeQuietly(BufferedWriter w) {
        if (w == null) {
            return;
        }
        try {
            synchronized (w) {
                w.close();
            }
        } catch (IOException e) {
            // ignore
        }
    }

    private class L2Processor implements PacketProcessor {
        @Override
        public void process(PacketContext context) {
            long t0 = System.nanoTime();
            packetInCount.incrementAndGet();
            if (context.isHandled()) {
                return;
            }
            InboundPacket pkt = context.inPacket();
            Ethernet eth = pkt.parsed();
            if (eth == null) {
                return;
            }
            DeviceId dev = pkt.receivedFrom().deviceId();
            PortNumber inPort = pkt.receivedFrom().port();
            MacAddress src = eth.getSourceMAC();
            MacAddress dst = eth.getDestinationMAC();

            Map<MacAddress, PortNumber> table = macToPort.computeIfAbsent(dev, k -> new ConcurrentHashMap<>());
            table.put(src, inPort);
            PortNumber outPort = table.getOrDefault(dst, PortNumber.FLOOD);

            if (!outPort.equals(PortNumber.FLOOD)) {
                FlowRule rule = DefaultFlowRule.builder()
                        .forDevice(dev)
                        .withSelector(DefaultTrafficSelector.builder()
                                .matchInPort(inPort).matchEthSrc(src).matchEthDst(dst).build())
                        .withTreatment(DefaultTrafficTreatment.builder().setOutput(outPort).build())
                        .withPriority(FLOW_PRIORITY)
                        .fromApp(appId)
                        .makePermanent()
                        .build();
                flowRuleService.applyFlowRules(rule);
                flowModCount.incrementAndGet();
            }
            context.treatmentBuilder().setOutput(outPort);
            context.send();
            context.block();
            double procMs = (System.nanoTime() - t0) / 1e6;

            int msgLen = pkt.unparsed() != null ? pkt.unparsed().limit() : -1;
            writeLine(flowEvents, wallTime() + "," + dpid(dev) + "," + mac(src) + "," + mac(dst) + ","
                    + inPort.toLong() + "," + String.format("%.6f", procMs) + "," + msgLen + "\n");
        }
    }

    private class TableMissInstaller implements DeviceListener {
        @Override
        public void event(DeviceEvent event) {
            if (event.type() == DeviceEvent.Type.DEVICE_ADDED
                    && deviceService.isAvailable(event.subject().id())) {
                installTableMiss(event.subject().id());
            }
        }
    }
}
