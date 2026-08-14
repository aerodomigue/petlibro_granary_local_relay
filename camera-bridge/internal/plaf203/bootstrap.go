package plaf203

import (
	"context"
	"encoding/binary"
	"fmt"
	"time"

	"github.com/AlexxIT/go2rtc/pkg/tutk"
)

const (
	defaultBootstrapTimeout  = 5 * time.Second
	bootstrapReplyTimeout    = 750 * time.Millisecond
	controlInnerLength       = 36
	controlChannelStream     = 0x1000
	controlChannelSystem     = 0x7000
	controlSetStream         = 0x0024
	controlGetFormat         = 0x032A
	controlStartVideo        = 0x01FF
	bootstrapAckMarker       = 0x09
	bootstrapHeartbeatMarker = 0x0A
	formatControlIndex       = 1
)

var streamControlHD = [8]byte{0x01, 0x00, 0xFF, 0x3F, 0x00, 0x00, 0x00, 0x00}

func (session *Session) bootstrap(ctx context.Context) (*VideoFrame, error) {
	timestampMillis := uint32(session.clock().UnixMilli())
	if err := session.sendLoginPair(timestampMillis); err != nil {
		return nil, err
	}
	if err := session.sendBody(encodeBootstrapHeartbeat(session.nextControlCounter(), timestampMillis)); err != nil {
		return nil, fmt.Errorf("send PLAF203 bootstrap heartbeat: %w", err)
	}
	if err := session.sendControl(controlChannelStream, 0, controlSetStream, streamControlHD[:]); err != nil {
		return nil, err
	}
	if err := session.sendControl(controlChannelSystem, formatControlIndex, controlGetFormat, nil); err != nil {
		return nil, err
	}
	// The V3.0.30 capture sends this acknowledgement immediately after
	// GET_FORMAT, before the corresponding device control replies arrive.
	if err := session.sendBody(encodeBootstrapAck(session.nextControlCounter(), formatControlIndex, uint16(session.clock().UnixMilli()))); err != nil {
		return nil, fmt.Errorf("send PLAF203 bootstrap acknowledgement: %w", err)
	}
	if err := session.waitForControlReplies(ctx); err != nil {
		return nil, err
	}
	if err := session.sendControl(controlChannelSystem, 2, controlStartVideo, nil); err != nil {
		return nil, err
	}
	return session.waitForVideo(ctx)
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
	if err := session.sendBody(encodeControlData(session.nextControlCounter(), channel, index, payload)); err != nil {
		return fmt.Errorf("send PLAF203 bootstrap control type=0x%04x: %w", controlType, err)
	}
	return nil
}

func (session *Session) waitForControlReplies(ctx context.Context) error {
	replyContext, cancel := context.WithTimeout(ctx, bootstrapReplyTimeout)
	defer cancel()
	const requiredReplies = 2
	replies := 0
	for replies < requiredReplies {
		packet, _, err := session.transport.Receive(replyContext)
		if err != nil {
			return fmt.Errorf("receive PLAF203 bootstrap control reply: %w", err)
		}
		inner, err := decodeDeviceSession(tutk.ReverseTransCodePartial(nil, packet), session.ID)
		if err != nil || !isControlReply(inner) {
			continue
		}
		replies++
	}
	return nil
}

func (session *Session) waitForVideo(ctx context.Context) (*VideoFrame, error) {
	for {
		packet, _, err := session.transport.Receive(ctx)
		if err != nil {
			return nil, fmt.Errorf("receive PLAF203 bootstrap media: %w", err)
		}
		frame, parseErr := session.media.HandlePacket(tutk.ReverseTransCodePartial(nil, packet), session.ID, session.clock())
		if parseErr != nil || frame == nil {
			continue
		}
		return frame, nil
	}
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

func (session *Session) nextControlCounter() uint16 {
	session.sendMu.Lock()
	defer session.sendMu.Unlock()
	counter := session.controlCounter
	session.controlCounter++
	return counter
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

func encodeBootstrapAck(counter uint16, subsequence uint16, tick uint16) []byte {
	body := make([]byte, 24)
	body[0] = bootstrapAckMarker
	body[2] = loginCommandVersion
	binary.LittleEndian.PutUint16(body[4:6], counter)
	binary.LittleEndian.PutUint16(body[8:10], 0x3FFF)
	binary.LittleEndian.PutUint16(body[10:12], 0x3FFF)
	binary.LittleEndian.PutUint32(body[12:16], 0xFFFF)
	binary.LittleEndian.PutUint16(body[16:18], subsequence)
	binary.LittleEndian.PutUint16(body[20:22], tick)
	return body
}

func isControlReply(inner []byte) bool {
	if len(inner) < controlInnerLength || inner[0] != 0x0C || inner[2] != loginCommandVersion {
		return false
	}
	channel := binary.LittleEndian.Uint16(inner[16:18])
	return channel == controlChannelStream || channel == controlChannelSystem
}
