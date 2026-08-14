package plaf203

import (
	"bytes"
	"context"
	"encoding/binary"
	"errors"
	"fmt"
	"net"
	"sync"
	"time"
)

const (
	loginRequestLength  = 598
	loginResponseLength = 88
	sessionHeaderLength = 28
	loginCommandLength  = 24
	loginFieldLength    = 257

	sessionFlags                     = 0x0A
	clientSessionMagicVersion        = 0x1C
	deviceSessionMagicVersion        = 0x1B
	clientSessionOpcode       uint16 = 0x0407
	deviceSessionOpcode       uint16 = 0x0408
	clientSessionSubtype      uint16 = 0x0021
	deviceLoginSubtype        uint16 = 0x0012

	loginCommandVersion = 0x0B
	loginSuccessCommand = 0x21
	loginConfigLength   = 32
	loginConfigMode     = 1
	loginConfigValue    = 4

	// These fixed protocol fields were observed in the V3.0.30 PCAP. They are
	// not user-configurable account credentials and are never logged.
	loginUsername = "admin"
	loginPassword = "888888"
)

var loginCapabilityBitmap = [4]byte{0xFB, 0x07, 0x1F, 0x00}

var (
	// ErrLoginRejected identifies a structurally valid response that does not
	// acknowledge the PLAF203 Session16 client-start request.
	ErrLoginRejected = errors.New("PLAF203 login was rejected")
)

// LoginVariant identifies the two mandatory Session16 client-start packets.
type LoginVariant uint8

const (
	LoginPrimary LoginVariant = iota
	LoginSecondary
)

// LoginRequest is one fixed-width PLAF203 Session16 client-start datagram
// before the official TUTK wire transform is applied.
type LoginRequest struct {
	SessionID       [8]byte
	Sequence        uint16
	Variant         LoginVariant
	TimestampMillis uint32
}

// LoginResponse is the verified 0x0408 acknowledgement of the client-start
// pair. No media parameters are interpreted here.
type LoginResponse struct {
	SessionID [8]byte
	Sequence  uint16
}

// Session owns one authenticated UDP transport and its bounded, diagnostic
// H.264 observation state. It never decodes or publishes media.
type Session struct {
	ID                    [8]byte
	Address               *net.UDPAddr
	transport             DatagramTransport
	clock                 func() time.Time
	sequence              uint16
	session25             session25State
	media                 *MediaReceiver
	observer              Observer
	sendMu                sync.Mutex
	mediaMu               sync.Mutex
	frameMu               sync.Mutex
	frameCallbacks        map[uint64]FrameCallback
	latestKeyframe        *VideoFrame
	nextFrameID           uint64
	audioMu               sync.Mutex
	audioCallbacks        map[uint64]AudioCallback
	nextAudioID           uint64
	audioEnabled          bool
	cancelReceive         context.CancelFunc
	lastMediaEvent        time.Time
	lastAudioEvent        time.Time
	lastUnknownMediaEvent time.Time
	closeOnce             sync.Once
	closeErr              error
}

// FrameCallback receives one complete H.264 access unit. Implementations must
// return quickly; the callback runs on the camera receive goroutine.
type FrameCallback func(*VideoFrame)

// AudioCallback receives one complete AAC-LC ADTS access unit. Implementations
// must return quickly; the callback runs on the camera receive goroutine.
type AudioCallback func(*AudioFrame)

// Close releases the authenticated transport. It is safe to call repeatedly.
func (session *Session) Close() error {
	if session == nil {
		return nil
	}
	session.closeOnce.Do(func() {
		_ = session.StopAudio()
		if session.cancelReceive != nil {
			session.cancelReceive()
		}
		if session.transport != nil {
			session.closeErr = session.transport.Close()
		}
	})
	return session.closeErr
}

// StartAudio enables the confirmed PLAF203 AAC media channel. It is idempotent
// and never opens a second feeder session.
func (session *Session) StartAudio() error {
	if session == nil {
		return errors.New("PLAF203 session is unavailable")
	}
	session.audioMu.Lock()
	defer session.audioMu.Unlock()
	if session.audioEnabled {
		return nil
	}
	if session.transport == nil || session.Address == nil {
		return errors.New("PLAF203 audio transport is unavailable")
	}
	if err := session.sendControl(controlChannelSystem, 0, controlStartAudio, make([]byte, audioControlArgumentSize)); err != nil {
		return fmt.Errorf("send PLAF203 AUDIOSTART: %w", err)
	}
	session.audioEnabled = true
	emit(session.observer, Event{State: StateStreaming, Address: cloneUDPAddress(session.Address), Step: "audio_start_tx", ControlType: controlStartAudio})
	return nil
}

// StopAudio disables the confirmed PLAF203 AAC media channel. It is idempotent
// and is called when the final local media consumer leaves or the session closes.
func (session *Session) StopAudio() error {
	if session == nil {
		return nil
	}
	session.audioMu.Lock()
	defer session.audioMu.Unlock()
	if !session.audioEnabled {
		return nil
	}
	if session.transport == nil || session.Address == nil {
		session.audioEnabled = false
		return nil
	}
	if err := session.sendControl(controlChannelSystem, 0, controlStopAudio, make([]byte, audioControlArgumentSize)); err != nil {
		return fmt.Errorf("send PLAF203 AUDIOSTOP: %w", err)
	}
	session.audioEnabled = false
	emit(session.observer, Event{State: StateStreaming, Address: cloneUDPAddress(session.Address), Step: "audio_stop_tx", ControlType: controlStopAudio})
	return nil
}

// MediaStats returns safe, aggregate media diagnostics for this session.
func (session *Session) MediaStats() MediaStats {
	if session == nil || session.media == nil {
		return MediaStats{}
	}
	return session.media.Snapshot()
}

// SubscribeFrames attaches a media sink to this session and returns an
// idempotent unsubscribe function. A newly attached sink receives the last
// complete keyframe immediately, so a consumer can decode without waiting for
// the next IDR interval.
func (session *Session) SubscribeFrames(callback FrameCallback) func() {
	if session == nil || callback == nil {
		return func() {}
	}
	session.frameMu.Lock()
	if session.frameCallbacks == nil {
		session.frameCallbacks = make(map[uint64]FrameCallback)
	}
	session.nextFrameID++
	callbackID := session.nextFrameID
	session.frameCallbacks[callbackID] = callback
	keyframe := cloneVideoFrame(session.latestKeyframe)
	session.frameMu.Unlock()
	if keyframe != nil {
		callback(keyframe)
	}
	var once sync.Once
	return func() {
		once.Do(func() {
			session.frameMu.Lock()
			delete(session.frameCallbacks, callbackID)
			session.frameMu.Unlock()
		})
	}
}

func (session *Session) publishFrame(frame *VideoFrame) {
	if frame == nil {
		return
	}
	session.frameMu.Lock()
	if frame.Keyframe {
		session.latestKeyframe = cloneVideoFrame(frame)
	}
	callbacks := make([]FrameCallback, 0, len(session.frameCallbacks))
	for _, callback := range session.frameCallbacks {
		callbacks = append(callbacks, callback)
	}
	session.frameMu.Unlock()
	for _, callback := range callbacks {
		callback(frame)
	}
}

// SubscribeAudio attaches an AAC media sink and returns an idempotent
// unsubscribe function. Audio is live-only; buffered replay would desync it.
func (session *Session) SubscribeAudio(callback AudioCallback) func() {
	if session == nil || callback == nil {
		return func() {}
	}
	session.audioMu.Lock()
	if session.audioCallbacks == nil {
		session.audioCallbacks = make(map[uint64]AudioCallback)
	}
	session.nextAudioID++
	callbackID := session.nextAudioID
	session.audioCallbacks[callbackID] = callback
	session.audioMu.Unlock()
	var once sync.Once
	return func() {
		once.Do(func() {
			session.audioMu.Lock()
			delete(session.audioCallbacks, callbackID)
			session.audioMu.Unlock()
		})
	}
}

func (session *Session) publishAudio(frame *AudioFrame) {
	if frame == nil {
		return
	}
	session.audioMu.Lock()
	callbacks := make([]AudioCallback, 0, len(session.audioCallbacks))
	for _, callback := range session.audioCallbacks {
		callbacks = append(callbacks, callback)
	}
	session.audioMu.Unlock()
	for _, callback := range callbacks {
		callback(frame)
	}
}

func cloneVideoFrame(frame *VideoFrame) *VideoFrame {
	if frame == nil {
		return nil
	}
	clone := *frame
	clone.Data = append([]byte(nil), frame.Data...)
	return &clone
}

// Encode serializes a full PLAF203 V3.0.30 Session16 client-start request.
func (request LoginRequest) Encode() ([]byte, error) {
	if request.Variant != LoginPrimary && request.Variant != LoginSecondary {
		return nil, fmt.Errorf("invalid PLAF203 login variant %d", request.Variant)
	}
	packet := make([]byte, loginRequestLength)
	packet[0] = 0x04
	packet[1] = 0x02
	packet[2] = clientSessionMagicVersion
	packet[3] = sessionFlags
	binary.LittleEndian.PutUint16(packet[4:], loginRequestLength-16)
	binary.LittleEndian.PutUint16(packet[6:], request.Sequence)
	binary.LittleEndian.PutUint16(packet[8:], clientSessionOpcode)
	binary.LittleEndian.PutUint16(packet[10:], clientSessionSubtype)
	copy(packet[12:28], sessionID16(request.SessionID))

	command := packet[sessionHeaderLength:]
	command[2] = loginCommandVersion
	if request.Variant == LoginPrimary {
		command[18] = 1
	} else {
		command[1] = 0x20
	}
	binary.LittleEndian.PutUint16(command[16:], loginRequestLength-52)
	binary.LittleEndian.PutUint32(command[20:], request.TimestampMillis)

	payload := command[loginCommandLength:]
	copy(payload[:loginFieldLength], loginUsername)
	copy(payload[loginFieldLength:2*loginFieldLength], loginPassword)
	loginConfig := payload[2*loginFieldLength:]
	loginConfig[0] = loginConfigMode
	binary.LittleEndian.PutUint32(loginConfig[4:], loginConfigValue)
	copy(loginConfig[8:], loginCapabilityBitmap[:])
	loginConfig[22] = 3
	loginConfig[28] = 1
	return packet, nil
}

// DecodeLoginResponse validates the full decrypted success acknowledgement for
// the expected session. Sending the requests alone never constitutes success.
func DecodeLoginResponse(packet []byte, expectedSessionID [8]byte) (LoginResponse, error) {
	if len(packet) != loginResponseLength {
		return LoginResponse{}, fmt.Errorf("%w: login response got=%d want=%d", ErrPacketTooShort, len(packet), loginResponseLength)
	}
	if packet[0] != 0x04 || packet[1] != 0x02 || packet[2] != deviceSessionMagicVersion || packet[3] != sessionFlags ||
		binary.LittleEndian.Uint16(packet[4:]) != loginResponseLength-16 ||
		binary.LittleEndian.Uint16(packet[8:]) != deviceSessionOpcode ||
		binary.LittleEndian.Uint16(packet[10:]) != deviceLoginSubtype {
		return LoginResponse{}, ErrUnexpectedPacket
	}
	if !bytes.Equal(packet[12:28], sessionID16(expectedSessionID)) {
		return LoginResponse{}, ErrUnexpectedPacket
	}
	command := packet[sessionHeaderLength:]
	if command[0] != 0 || command[1] != loginSuccessCommand || command[2] != loginCommandVersion {
		return LoginResponse{}, ErrLoginRejected
	}
	return LoginResponse{
		SessionID: expectedSessionID,
		Sequence:  binary.LittleEndian.Uint16(packet[6:]),
	}, nil
}

func sessionID16(sessionID [8]byte) []byte {
	encoded := make([]byte, 16)
	copy(encoded[8:], sessionID[:])
	copy(encoded[:2], sessionID[:2])
	encoded[4] = 0x0C
	return encoded
}
