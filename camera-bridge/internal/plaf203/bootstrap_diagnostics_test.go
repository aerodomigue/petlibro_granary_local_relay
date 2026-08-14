package plaf203

import (
	"net"
	"strings"
	"testing"

	"github.com/AlexxIT/go2rtc/pkg/tutk"
)

func TestBootstrapDiagnosticsClassifiesAndDeduplicatesReceiveEvents(t *testing.T) {
	sessionID := [8]byte{1, 2, 3, 4, 5, 6, 7, 8}
	peer := &net.UDPAddr{IP: net.ParseIP("192.0.2.20"), Port: 41135}
	events := make([]Event, 0, 3)
	diagnostics := newBootstrapDiagnostics(sessionID, func(event Event) {
		events = append(events, event)
	})

	packet := tutk.TransCodePartial(nil, testControlReply(sessionID, controlChannelSystem, 1, controlGetFormatReply))
	for range 2 {
		decoded, details := diagnostics.observe(packet, peer)
		if details.classification != "control_reply" || len(decoded) != len(packet) {
			t.Fatalf("details=%+v decoded=%d", details, len(decoded))
		}
		diagnostics.ignore(details, "not_control_reply")
	}
	diagnostics.emitTimeout()

	if len(events) != 3 {
		t.Fatalf("events=%+v", events)
	}
	if events[0].Step != "rx" || events[0].Opcode != deviceSessionOpcode || events[0].SessionChannel != controlChannelSystem || events[0].Sequence != 0 {
		t.Fatalf("receive event=%+v", events[0])
	}
	if events[1].Step != "ignore" || events[1].Reason != "not_control_reply" {
		t.Fatalf("ignore event=%+v", events[1])
	}
	if events[2].Step != "timeout" || events[2].PacketCount != 2 || events[2].ByteCount != uint64(len(packet))*2 ||
		!strings.Contains(events[2].Types, "bytes=") || !strings.Contains(events[2].Rejected, "not_control_reply=2") {
		t.Fatalf("timeout event=%+v", events[2])
	}
}

func TestInspectBootstrapPacketExplainsSessionMismatch(t *testing.T) {
	expectedSessionID := [8]byte{1, 2, 3, 4, 5, 6, 7, 8}
	wrongSessionID := [8]byte{8, 7, 6, 5, 4, 3, 2, 1}
	packet := testControlReply(wrongSessionID, controlChannelStream, 0, controlSetStreamReply)
	details := inspectBootstrapPacket(packet, &net.UDPAddr{IP: net.ParseIP("192.0.2.20"), Port: 41135}, expectedSessionID)
	if details.classification != "session_id_mismatch" || details.opcode != deviceSessionOpcode || details.sequence != 0 {
		t.Fatalf("details=%+v", details)
	}
}
