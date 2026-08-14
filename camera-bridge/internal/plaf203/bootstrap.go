package plaf203

import (
	"context"
	"encoding/binary"
	"encoding/hex"
	"errors"
	"fmt"
	"net"
	"time"

	"github.com/AlexxIT/go2rtc/pkg/tutk"
)

const (
	defaultBootstrapTimeout   = 5 * time.Second
	bootstrapReplyTimeout     = 750 * time.Millisecond
	controlInnerLength        = 36
	controlChannelStream      = 0x1000
	controlChannelSystem      = 0x7000
	controlSetStream          = 0x0024
	controlSetStreamReply     = controlSetStream + 1
	controlGetFormat          = 0x032A
	controlGetFormatReply     = controlGetFormat + 1
	controlStartVideo         = 0x01FF
	controlStartVideoReply    = controlStartVideo + 1
	streamStartArgumentSize   = 8
	keepaliveResponseOpcode   = 0x0428
	keepaliveRequestOpcode    = 0x0427
	keepaliveResponseSubtype  = 0x0012
	keepaliveRequestSubtype   = 0x0021
	keepalivePacketLength     = 24
	sessionHeartbeatLength    = 16
	sessionHeartbeatAckLength = 20
	bootstrapHeartbeatMarker  = 0x0A
	formatControlIndex        = 1
)

var streamControlHD = [8]byte{0x01, 0x00, 0xFF, 0x3F, 0x00, 0x00, 0x00, 0x00}

func (session *Session) bootstrap(ctx context.Context) (_ *VideoFrame, returnErr error) {
	diagnostics := newBootstrapDiagnostics(session.ID, session.observer)
	defer func() {
		if errors.Is(returnErr, context.DeadlineExceeded) {
			diagnostics.emitTimeout()
		}
	}()

	timestampMillis := uint32(session.clock().UnixMilli())
	if err := session.sendLoginPair(timestampMillis); err != nil {
		return nil, err
	}
	if err := session.sendBody(encodeBootstrapHeartbeat(session.nextSession25ControlCounter(), timestampMillis)); err != nil {
		return nil, fmt.Errorf("send PLAF203 bootstrap heartbeat: %w", err)
	}
	if err := session.sendSession25Counters(diagnostics); err != nil {
		return nil, fmt.Errorf("send PLAF203 initial Session25 counters: %w", err)
	}
	if err := session.sendControl(controlChannelStream, 0, controlSetStream, streamControlHD[:]); err != nil {
		return nil, err
	}
	if err := session.sendSession25Counters(diagnostics); err != nil {
		return nil, fmt.Errorf("send PLAF203 post-stream-control counters: %w", err)
	}
	if err := session.sendControl(controlChannelSystem, formatControlIndex, controlGetFormat, nil); err != nil {
		return nil, err
	}
	if err := session.waitForControlReplies(ctx, diagnostics); err != nil {
		return nil, err
	}
	if err := session.sendStartVideo(); err != nil {
		return nil, err
	}
	// The official application sends another counters frame immediately after
	// IPCAM_START. This is Session25 flow control, not an additional IOCtrl.
	if err := session.sendSession25Counters(diagnostics); err != nil {
		return nil, fmt.Errorf("send PLAF203 post-start counters: %w", err)
	}
	return session.waitForVideo(ctx, diagnostics)
}

// sendStartVideo builds and reports the complete IPCAM_START datagram before
// applying the same TUTK transform used by every PLAF203 Session16 send.
func (session *Session) sendStartVideo() error {
	// IOTYPE_USER_IPCAM_START uses SMsgAVIoctrlAVStream: an unsigned
	// 32-bit channel followed by four reserved bytes. Both are zero for the
	// primary stream. The IOCtrl framing adds controlStartVideo before this
	// eight-byte command argument.
	payload := make([]byte, 4+streamStartArgumentSize)
	binary.LittleEndian.PutUint32(payload, controlStartVideo)
	body := encodeControlData(session.nextSession25ControlCounter(), controlChannelSystem, 2, payload)
	packet := encodeClientSessionPacket(session.ID, session.nextSequence(), body)
	wire := tutk.TransCodePartial(nil, packet)
	emit(session.observer, Event{
		State:          StateBootstrapping,
		Address:        cloneUDPAddress(session.Address),
		Step:           "stream_start_packet",
		PacketLength:   len(wire),
		BodyLength:     len(body),
		Opcode:         binary.LittleEndian.Uint16(packet[8:10]),
		Sequence:       binary.LittleEndian.Uint16(packet[6:8]),
		SessionChannel: binary.LittleEndian.Uint16(body[16:18]),
		SessionCommand: binary.LittleEndian.Uint16(body[:2]),
		ControlType:    controlStartVideo,
		DecodedHex:     hex.EncodeToString(packet),
		WireHex:        hex.EncodeToString(wire),
	})
	if err := session.transport.SendTo(wire, session.Address); err != nil {
		return fmt.Errorf("send PLAF203 bootstrap control type=0x%04x: %w", controlStartVideo, err)
	}
	emit(session.observer, Event{
		State:       StateBootstrapping,
		Address:     cloneUDPAddress(session.Address),
		Step:        "stream_start_tx",
		ControlType: controlStartVideo,
	})
	return nil
}

func (session *Session) sendLoginPair(timestampMillis uint32) error {
	for _, variant := range []LoginVariant{LoginPrimary, LoginSecondary} {
		request := LoginRequest{
			SessionID:       session.ID,
			Sequence:        session.nextSequence(),
			Variant:         variant,
			TimestampMillis: timestampMillis + uint32(variant),
		}
		packet, err := request.Encode()
		if err != nil {
			return err
		}
		if err := session.sendPacket(packet); err != nil {
			return fmt.Errorf("send PLAF203 bootstrap client-start: %w", err)
		}
	}
	return nil
}

func (session *Session) sendControl(channel uint16, index uint32, controlType uint32, controlData []byte) error {
	payload := make([]byte, 4+len(controlData))
	binary.LittleEndian.PutUint32(payload, controlType)
	copy(payload[4:], controlData)
	if err := session.sendBody(encodeControlData(session.nextSession25ControlCounter(), channel, index, payload)); err != nil {
		return fmt.Errorf("send PLAF203 bootstrap control type=0x%04x: %w", controlType, err)
	}
	return nil
}

func (session *Session) waitForControlReplies(ctx context.Context, diagnostics *bootstrapDiagnostics) error {
	replyContext, cancel := context.WithTimeout(ctx, bootstrapReplyTimeout)
	defer cancel()
	expectedReplies := map[controlReplyKey]struct{}{
		{channel: controlChannelStream, controlType: controlSetStreamReply}: {},
		{channel: controlChannelSystem, controlType: controlGetFormatReply}: {},
	}
	for len(expectedReplies) > 0 {
		packet, peer, err := session.transport.Receive(replyContext)
		if err != nil {
			return fmt.Errorf("receive PLAF203 bootstrap control reply: %w", err)
		}
		decoded, details := diagnostics.observe(packet, peer)
		if handled, heartbeatErr := session.replySessionHeartbeat(decoded, peer, diagnostics); heartbeatErr != nil {
			return heartbeatErr
		} else if handled {
			if countersErr := session.sendSession25Counters(diagnostics); countersErr != nil {
				return countersErr
			}
			continue
		}
		inner, decodeErr := decodeDeviceSession(decoded, session.ID)
		if decodeErr != nil {
			diagnostics.ignore(details, details.classification)
			continue
		}
		if handled, countersErr := session.replySession25Counters(inner, peer, diagnostics); countersErr != nil {
			return countersErr
		} else if handled {
			continue
		}
		reply, valid := parseControlReply(inner)
		if !valid {
			diagnostics.ignore(details, "not_control_reply")
			continue
		}
		session.session25.noteReceivedControlReply()
		if reply.channel == controlChannelStream {
			if countersErr := session.sendSession25Counters(diagnostics); countersErr != nil {
				return countersErr
			}
		}
		key := controlReplyKey{channel: reply.channel, controlType: reply.controlType}
		if _, expected := expectedReplies[key]; !expected {
			diagnostics.ignore(details, "unexpected_control_reply")
			continue
		}
		delete(expectedReplies, key)
	}
	return nil
}

func (session *Session) waitForVideo(ctx context.Context, diagnostics *bootstrapDiagnostics) (*VideoFrame, error) {
	for {
		packet, peer, err := session.transport.Receive(ctx)
		if err != nil {
			return nil, fmt.Errorf("receive PLAF203 bootstrap media: %w", err)
		}
		decoded, details := diagnostics.observe(packet, peer)
		if handled, keepaliveErr := session.replyKeepalive(decoded, peer); keepaliveErr != nil {
			return nil, keepaliveErr
		} else if handled {
			continue
		}
		if handled, heartbeatErr := session.replySessionHeartbeat(decoded, peer, diagnostics); heartbeatErr != nil {
			return nil, heartbeatErr
		} else if handled {
			continue
		}
		if inner, decodeErr := decodeDeviceSession(decoded, session.ID); decodeErr == nil {
			if handled, countersErr := session.replySession25Counters(inner, peer, diagnostics); countersErr != nil {
				return nil, countersErr
			} else if handled {
				continue
			}
			if reply, valid := parseControlReply(inner); valid && reply.channel == controlChannelSystem && reply.controlType == controlStartVideoReply {
				emit(session.observer, Event{
					State:       StateBootstrapping,
					Address:     cloneUDPAddress(peer),
					Step:        "stream_start_ack",
					ControlType: reply.controlType,
				})
				continue
			}
		}
		frame, parseErr := session.media.HandlePacket(decoded, session.ID, session.clock())
		if parseErr != nil {
			diagnostics.ignore(details, "media_parse_error")
			continue
		}
		if frame == nil {
			if details.classification == "media_fragment" {
				diagnostics.media(details)
				diagnostics.ignore(details, "media_fragment_incomplete")
			} else {
				diagnostics.ignore(details, details.classification)
			}
			continue
		}
		diagnostics.media(details)
		session.noteSession25Media(decoded)
		return frame, nil
	}
}

// replySession25Counters accepts the 24-byte Session25 09000b00 counters
// packet previously rejected as a short body and returns a dynamic counters
// acknowledgement. The response advances live per-session state.
func (session *Session) replySession25Counters(inner []byte, peer *net.UDPAddr, diagnostics *bootstrapDiagnostics) (bool, error) {
	counters, recognized := decodeSession25Counters(inner)
	if !recognized {
		return false, nil
	}
	diagnostics.emitLimited("session25_counters_rx", 4, session25Event("session25_counters_rx", peer, counters, session.session25))
	if err := session.sendSession25Counters(diagnostics); err != nil {
		return true, err
	}
	return true, nil
}

func (session *Session) sendSession25Counters(diagnostics *bootstrapDiagnostics) error {
	body, counters := session.session25.nextCounters(uint16(session.clock().UnixMilli()))
	if err := session.sendBody(body); err != nil {
		return fmt.Errorf("send PLAF203 Session25 counters: %w", err)
	}
	diagnostics.emitLimited("session25_counters_tx", 4, session25Event("session25_counters_tx", session.Address, counters, session.session25))
	return nil
}

func (session *Session) noteSession25Media(packet []byte) {
	inner, err := decodeDeviceSession(packet, session.ID)
	if err != nil || len(inner) < controlInnerLength || inner[0] != 0x0C || inner[2] != loginCommandVersion {
		return
	}
	session.session25.noteReceivedMedia(binary.LittleEndian.Uint16(inner[18:20]))
}

func session25Event(step string, address *net.UDPAddr, counters session25Counters, state session25State) Event {
	return Event{
		State:                StateBootstrapping,
		Address:              cloneUDPAddress(address),
		Step:                 step,
		Session25SeqSendCmd1: counters.seqSendCmd1,
		Session25SeqSendCmd2: state.seqSendCmd2,
		Session25SeqRecvCmd2: counters.seqRecvCmd2,
		Session25SeqRecvPkt0: counters.seqRecvPkt0,
		Session25SeqRecvPkt1: counters.seqRecvPkt1,
		Session25SeqSendCnt:  counters.seqSendCnt,
	}
}

// replySessionHeartbeat acknowledges the short Session25-compatible 0A08
// packet emitted by PLAF203 V3.0.30 after login. The body layout follows the
// generic TUTK Session25 msgAck0A08 exchange and was verified against the
// captured PLAF203 packet before applying the device-specific Session16 wrap.
func (session *Session) replySessionHeartbeat(packet []byte, peer *net.UDPAddr, diagnostics *bootstrapDiagnostics) (bool, error) {
	inner, decodeErr := decodeDeviceSession(packet, session.ID)
	if decodeErr != nil {
		return false, nil
	}
	ack, recognized := encodeSessionHeartbeatAck(inner)
	if !recognized {
		return false, nil
	}
	diagnostics.emitOnce("session_heartbeat_rx", Event{
		State:        StateBootstrapping,
		Address:      cloneUDPAddress(peer),
		Step:         "session_heartbeat_rx",
		PacketLength: len(inner),
		Opcode:       deviceSessionOpcode,
	})
	if sendErr := session.sendBody(ack); sendErr != nil {
		return true, fmt.Errorf("send PLAF203 session heartbeat acknowledgement: %w", sendErr)
	}
	diagnostics.emitOnce("session_heartbeat_tx", Event{
		State:        StateBootstrapping,
		Address:      cloneUDPAddress(session.Address),
		Step:         "session_heartbeat_tx",
		PacketLength: len(ack),
		Opcode:       clientSessionOpcode,
	})
	return true, nil
}

// replyKeepalive handles the 24-byte IOTC keepalive response by preserving
// the PLAF203 envelope and echo payload while changing only opcode/subtype.
func (session *Session) replyKeepalive(packet []byte, peer *net.UDPAddr) (bool, error) {
	reply, recognized := encodeKeepaliveReply(packet)
	if !recognized {
		return false, nil
	}
	emit(session.observer, Event{
		State:        StateBootstrapping,
		Address:      cloneUDPAddress(peer),
		Step:         "keepalive_rx",
		PacketLength: len(packet),
		Opcode:       keepaliveResponseOpcode,
	})
	if err := session.transport.SendTo(tutk.TransCodePartial(nil, reply), session.Address); err != nil {
		return true, fmt.Errorf("send PLAF203 keepalive reply: %w", err)
	}
	emit(session.observer, Event{
		State:        StateBootstrapping,
		Address:      cloneUDPAddress(session.Address),
		Step:         "keepalive_tx",
		PacketLength: len(reply),
		Opcode:       keepaliveRequestOpcode,
	})
	return true, nil
}

func encodeKeepaliveReply(packet []byte) ([]byte, bool) {
	if len(packet) != keepalivePacketLength || packet[0] != 0x04 || packet[1] != 0x02 || packet[2] != deviceSessionMagicVersion || packet[3] != sessionFlags || binary.LittleEndian.Uint16(packet[4:6]) != 8 || binary.LittleEndian.Uint16(packet[8:10]) != keepaliveResponseOpcode || binary.LittleEndian.Uint16(packet[10:12]) != keepaliveResponseSubtype {
		return nil, false
	}
	reply := append([]byte(nil), packet...)
	binary.LittleEndian.PutUint16(reply[8:10], keepaliveRequestOpcode)
	binary.LittleEndian.PutUint16(reply[10:12], keepaliveRequestSubtype)
	return reply, true
}

func encodeSessionHeartbeatAck(inner []byte) ([]byte, bool) {
	if len(inner) != sessionHeartbeatLength || inner[0] != bootstrapHeartbeatMarker || inner[1] != 0x08 || inner[2] != loginCommandVersion {
		return nil, false
	}
	ack := make([]byte, sessionHeartbeatAckLength)
	copy(ack[:4], "\x0b\x00\x0b\x00")
	ack[6] = 1
	copy(ack[8:10], inner[8:10])
	ack[10] = 3
	ack[12] = 3
	return ack, true
}

func (session *Session) sendBody(body []byte) error {
	packet := encodeClientSessionPacket(session.ID, session.nextSequence(), body)
	return session.sendPacket(packet)
}

func (session *Session) sendPacket(packet []byte) error {
	return session.transport.SendTo(tutk.TransCodePartial(nil, packet), session.Address)
}

func (session *Session) nextSequence() uint16 {
	session.sendMu.Lock()
	defer session.sendMu.Unlock()
	sequence := session.sequence
	session.sequence++
	return sequence
}

func (session *Session) nextSession25ControlCounter() uint16 {
	session.sendMu.Lock()
	defer session.sendMu.Unlock()
	return session.session25.nextOutboundCommand()
}

func encodeClientSessionPacket(sessionID [8]byte, sequence uint16, body []byte) []byte {
	packet := make([]byte, sessionHeaderLength+len(body))
	packet[0] = 0x04
	packet[1] = 0x02
	packet[2] = clientSessionMagicVersion
	packet[3] = sessionFlags
	binary.LittleEndian.PutUint16(packet[4:], uint16(len(packet)-16))
	binary.LittleEndian.PutUint16(packet[6:], sequence)
	binary.LittleEndian.PutUint16(packet[8:], clientSessionOpcode)
	binary.LittleEndian.PutUint16(packet[10:], clientSessionSubtype)
	copy(packet[12:28], sessionID16(sessionID))
	copy(packet[sessionHeaderLength:], body)
	return packet
}

func decodeDeviceSession(packet []byte, expectedSessionID [8]byte) ([]byte, error) {
	if len(packet) < sessionHeaderLength || packet[0] != 0x04 || packet[1] != 0x02 || packet[2] != deviceSessionMagicVersion || packet[3] != sessionFlags || binary.LittleEndian.Uint16(packet[4:6]) != uint16(len(packet)-16) || binary.LittleEndian.Uint16(packet[8:10]) != deviceSessionOpcode || binary.LittleEndian.Uint16(packet[10:12]) != deviceLoginSubtype || string(packet[12:28]) != string(sessionID16(expectedSessionID)) {
		return nil, ErrUnexpectedPacket
	}
	return packet[sessionHeaderLength:], nil
}

func encodeControlData(counter uint16, channel uint16, index uint32, payload []byte) []byte {
	body := make([]byte, controlInnerLength+len(payload))
	body[0] = 0x0C
	body[2] = loginCommandVersion
	binary.LittleEndian.PutUint16(body[4:6], counter)
	binary.LittleEndian.PutUint16(body[16:18], channel)
	binary.LittleEndian.PutUint32(body[20:24], 1)
	binary.LittleEndian.PutUint32(body[24:28], uint32(len(payload)))
	binary.LittleEndian.PutUint32(body[28:32], index)
	copy(body[controlInnerLength:], payload)
	return body
}

func encodeBootstrapHeartbeat(counter uint16, timestamp uint32) []byte {
	body := make([]byte, 16)
	body[0] = bootstrapHeartbeatMarker
	body[1] = 0x08
	body[2] = loginCommandVersion
	binary.LittleEndian.PutUint16(body[4:6], counter)
	binary.LittleEndian.PutUint32(body[8:12], timestamp)
	return body
}

type controlReplyKey struct {
	channel     uint16
	controlType uint32
}

func parseControlReply(inner []byte) (controlReplyKey, bool) {
	if len(inner) < controlInnerLength || inner[0] != 0x0C || inner[2] != loginCommandVersion {
		return controlReplyKey{}, false
	}
	channel := binary.LittleEndian.Uint16(inner[16:18])
	if channel != controlChannelStream && channel != controlChannelSystem {
		return controlReplyKey{}, false
	}
	if len(inner) < controlInnerLength+4 {
		return controlReplyKey{}, false
	}
	return controlReplyKey{channel: channel, controlType: binary.LittleEndian.Uint32(inner[controlInnerLength:])}, true
}
