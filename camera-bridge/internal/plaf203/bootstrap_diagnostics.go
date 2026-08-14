package plaf203

import (
	"bytes"
	"encoding/binary"
	"fmt"
	"net"
	"sort"
	"strings"

	"github.com/AlexxIT/go2rtc/pkg/tutk"
)

// bootstrapPacketDetails contains only protocol headers and counters suitable
// for bounded diagnostics. It never retains or emits packet payload bytes.
type bootstrapPacketDetails struct {
	peer           *net.UDPAddr
	packetLength   int
	opcode         uint16
	sequence       uint16
	sessionChannel uint16
	sessionCommand uint16
	controlType    uint32
	classification string
}

func (details bootstrapPacketDetails) typeKey() string {
	return fmt.Sprintf(
		"bytes=%d/opcode=0x%04x/channel=0x%04x/cmd=0x%04x",
		details.packetLength,
		details.opcode,
		details.sessionChannel,
		details.sessionCommand,
	)
}

func (details bootstrapPacketDetails) event(step string, reason string) Event {
	return Event{
		State:          StateBootstrapping,
		Address:        cloneUDPAddress(details.peer),
		Step:           step,
		PacketLength:   details.packetLength,
		Opcode:         details.opcode,
		Sequence:       details.sequence,
		SessionChannel: details.sessionChannel,
		SessionCommand: details.sessionCommand,
		ControlType:    details.controlType,
		Reason:         reason,
	}
}

// bootstrapDiagnostics aggregates bootstrap receive activity and emits only
// the first occurrence of each packet or rejection shape.
type bootstrapDiagnostics struct {
	sessionID [8]byte
	observer  Observer

	packetCount  uint64
	byteCount    uint64
	types        map[string]uint64
	rejected     map[string]uint64
	emitted      map[string]struct{}
	emittedCount map[string]uint
}

func newBootstrapDiagnostics(sessionID [8]byte, observer Observer) *bootstrapDiagnostics {
	return &bootstrapDiagnostics{
		sessionID:    sessionID,
		observer:     observer,
		types:        make(map[string]uint64),
		rejected:     make(map[string]uint64),
		emitted:      make(map[string]struct{}),
		emittedCount: make(map[string]uint),
	}
}

// observe applies the required reversible wire transform, records an inbound
// datagram before Session16 filtering, and returns the decoded bytes.
func (diagnostics *bootstrapDiagnostics) observe(packet []byte, peer *net.UDPAddr) ([]byte, bootstrapPacketDetails) {
	decoded := tutk.ReverseTransCodePartial(nil, packet)
	details := inspectBootstrapPacket(decoded, peer, diagnostics.sessionID)
	diagnostics.packetCount++
	diagnostics.byteCount += uint64(len(packet))
	diagnostics.types[details.typeKey()]++
	diagnostics.emitOnce("rx/"+details.typeKey(), details.event("rx", ""))
	return decoded, details
}

// ignore records a packet that the active bootstrap phase cannot consume.
func (diagnostics *bootstrapDiagnostics) ignore(details bootstrapPacketDetails, reason string) {
	if reason == "" {
		reason = "unclassified"
	}
	diagnostics.rejected[reason]++
	diagnostics.emitOnce("ignore/"+details.typeKey()+"/"+reason, details.event("ignore", reason))
}

// media records the first observed media fragment shape without retaining its payload.
func (diagnostics *bootstrapDiagnostics) media(details bootstrapPacketDetails) {
	diagnostics.emitOnce("media/"+details.typeKey(), details.event("media_rx", ""))
}

// emitTimeout reports aggregate activity without exposing payload contents.
func (diagnostics *bootstrapDiagnostics) emitTimeout() {
	emit(diagnostics.observer, Event{
		State:       StateBootstrapping,
		Step:        "timeout",
		PacketCount: diagnostics.packetCount,
		ByteCount:   diagnostics.byteCount,
		Types:       formatBootstrapCounters(diagnostics.types),
		Rejected:    formatBootstrapCounters(diagnostics.rejected),
	})
}

func (diagnostics *bootstrapDiagnostics) emitOnce(key string, event Event) {
	if _, alreadyEmitted := diagnostics.emitted[key]; alreadyEmitted {
		return
	}
	diagnostics.emitted[key] = struct{}{}
	emit(diagnostics.observer, event)
}

// emitLimited publishes only the first limit occurrences of a protocol shape.
// It keeps counter diagnostics useful without turning a high-rate flow-control
// exchange into a log flood.
func (diagnostics *bootstrapDiagnostics) emitLimited(key string, limit uint, event Event) {
	if diagnostics.emittedCount[key] >= limit {
		return
	}
	diagnostics.emittedCount[key]++
	emit(diagnostics.observer, event)
}

func inspectBootstrapPacket(packet []byte, peer *net.UDPAddr, expectedSessionID [8]byte) bootstrapPacketDetails {
	details := bootstrapPacketDetails{
		peer:           peer,
		packetLength:   len(packet),
		classification: "outer_truncated",
	}
	if len(packet) >= 8 {
		details.sequence = binary.LittleEndian.Uint16(packet[6:8])
	}
	if len(packet) >= 10 {
		details.opcode = binary.LittleEndian.Uint16(packet[8:10])
	}
	if len(packet) < sessionHeaderLength {
		return details
	}
	if packet[0] != 0x04 || packet[1] != 0x02 || packet[2] != deviceSessionMagicVersion {
		details.classification = "outer_magic_mismatch"
		return details
	}
	if packet[3] != sessionFlags {
		details.classification = "outer_flags_mismatch"
		return details
	}
	if binary.LittleEndian.Uint16(packet[4:6]) != uint16(len(packet)-16) {
		details.classification = "outer_length_mismatch"
		return details
	}
	if details.opcode != deviceSessionOpcode || binary.LittleEndian.Uint16(packet[10:12]) != deviceLoginSubtype {
		details.classification = "outer_opcode_mismatch"
		return details
	}
	if !bytes.Equal(packet[12:28], sessionID16(expectedSessionID)) {
		details.classification = "session_id_mismatch"
		return details
	}

	inner := packet[sessionHeaderLength:]
	if len(inner) < 2 {
		details.classification = "session_body_truncated"
		return details
	}
	details.sessionCommand = binary.LittleEndian.Uint16(inner[:2])
	if _, isCounters := decodeSession25Counters(inner); isCounters {
		details.classification = "session25_counters"
		return details
	}
	if len(inner) < controlInnerLength {
		details.classification = "session_body_short"
		return details
	}
	details.sessionChannel = binary.LittleEndian.Uint16(inner[16:18])
	if len(inner) >= controlInnerLength+4 {
		details.controlType = binary.LittleEndian.Uint32(inner[controlInnerLength : controlInnerLength+4])
	}
	if inner[0] != 0x0C || inner[2] != loginCommandVersion {
		details.classification = "session_command_mismatch"
		return details
	}
	switch details.sessionChannel {
	case controlChannelStream, controlChannelSystem:
		details.classification = "control_reply"
	case uint16(h264MainChannel), uint16(h264SubChannel):
		details.classification = "media_fragment"
	default:
		details.classification = "session_channel_unknown"
	}
	return details
}

func formatBootstrapCounters(counters map[string]uint64) string {
	if len(counters) == 0 {
		return "{}"
	}
	keys := make([]string, 0, len(counters))
	for key := range counters {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	parts := make([]string, 0, len(keys))
	for _, key := range keys {
		parts = append(parts, fmt.Sprintf("%s=%d", key, counters[key]))
	}
	return "{" + strings.Join(parts, ",") + "}"
}
