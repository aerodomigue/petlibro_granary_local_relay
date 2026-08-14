// Package bridge implements the local, auditable registration API for cameras.
package bridge

import (
	"context"
	"errors"
	"log"
	"net"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/AlexxIT/go2rtc/pkg/rtsp"
	"github.com/AlexxIT/go2rtc/pkg/tutk"
	"github.com/aerodomigue/petlibro-camera-bridge/internal/plaf203"
)

const (
	petlibroUIDLength = 20
	streamPrefix      = "plaf203_"
)

var (
	// ErrInvalidDeviceID rejects identifiers that cannot safely form a stream name.
	ErrInvalidDeviceID = errors.New("device_id must contain only letters, digits, '_' or '-'")
	// ErrInvalidUID rejects anything other than the exact PLAF203 UID shape observed in MQTT.
	ErrInvalidUID = errors.New("uid must be exactly 20 printable ASCII characters")
	// ErrInvalidIP rejects a non-IPv4 feeder address supplied by the relay.
	ErrInvalidIP = errors.New("ip must be a valid IPv4 address")
	// ErrDeviceNotFound rejects an explicit connection request for an unregistered device.
	ErrDeviceNotFound = errors.New("device is not registered")
)

// Device is the non-sensitive state exposed by the bridge API.
// The UID remains internal to the bridge; callers only learn that it was registered.
type Device struct {
	DeviceID         string               `json:"device_id"`
	UIDLearned       bool                 `json:"uid_learned"`
	IP               string               `json:"ip,omitempty"`
	Stream           string               `json:"stream"`
	StreamAvailable  bool                 `json:"stream_available"`
	Reason           string               `json:"reason"`
	ConnectionState  plaf203.SessionState `json:"connection_state"`
	LastError        string               `json:"last_error,omitempty"`
	LastTransitionAt time.Time            `json:"last_transition_at"`
	UpdatedAt        time.Time            `json:"updated_at"`
	VideoCodec       string               `json:"video_codec,omitempty"`
	AudioCodec       string               `json:"audio_codec,omitempty"`
	FramesReceived   uint64               `json:"frames_received"`
	BytesReceived    uint64               `json:"bytes_received"`
	LastFrameAt      time.Time            `json:"last_frame_at,omitempty"`
	MediaConsumers   int                  `json:"media_consumers"`
}

type deviceRecord struct {
	device           Device
	uid              string
	ip               net.IP
	cancel           context.CancelFunc
	session          *plaf203.Session
	media            *mediaPublisher
	mediaUnsubscribe func()
	attemptID        uint64
	connectActive    bool
}

// Registry keeps device registrations isolated by device ID and owns one
// bounded, explicitly requested PLAF203 preamble attempt per device.
type Registry struct {
	mu        sync.RWMutex
	devices   map[string]deviceRecord
	connector plaf203.Connector
}

// NewRegistry creates an empty in-memory registry. Durable UID storage is owned
// by the relay State Shadow; it re-registers devices after restart.
func NewRegistry() *Registry {
	return NewRegistryWithBroadcastFallback(true)
}

// NewRegistryWithBroadcastFallback configures the optional fallback used only
// after a known-IP unicast discovery attempt fails.
func NewRegistryWithBroadcastFallback(broadcastFallback bool) *Registry {
	connector := plaf203.NewDirectConnector()
	connector.BroadcastFallback = broadcastFallback
	return NewRegistryWithConnector(connector)
}

// NewRegistryWithConnector allows protocol behavior to be isolated by fakes in tests.
func NewRegistryWithConnector(connector plaf203.Connector) *Registry {
	return &Registry{
		devices:   make(map[string]deviceRecord),
		connector: connector,
	}
}

// Upsert validates and records one UID plus its optional feeder IPv4 address.
// Updating a known address never interrupts an active camera session.
func (r *Registry) Upsert(deviceID string, uid string, ip string) (Device, error) {
	if !validDeviceID(deviceID) {
		return Device{}, ErrInvalidDeviceID
	}
	if !validUID(uid) {
		return Device{}, ErrInvalidUID
	}
	parsedIP, err := parseIPv4(ip)
	if err != nil {
		return Device{}, err
	}

	// Keep the official wire-transform dependency compile-checked. The
	// Petlibro-specific protocol remains a separately tested implementation and
	// never borrows third-party fork code at runtime.
	_ = tutk.TransCodePartial

	r.mu.Lock()
	record, exists := r.devices[deviceID]
	now := time.Now().UTC()
	if !exists {
		record.device = Device{
			DeviceID:         deviceID,
			UIDLearned:       true,
			Stream:           streamPrefix + deviceID,
			StreamAvailable:  false,
			Reason:           "plaf203_h264_observation_only",
			ConnectionState:  plaf203.StateIdle,
			LastTransitionAt: now,
		}
		record.media = newMediaPublisher()
	}
	record.uid = uid
	if parsedIP != nil {
		record.ip = parsedIP
		record.device.IP = parsedIP.String()
	}
	record.device.UIDLearned = true
	record.device.UpdatedAt = now
	r.devices[deviceID] = record
	r.mu.Unlock()
	return record.device, nil
}

// Delete removes one device registration. It is idempotent and never contacts a camera.
func (r *Registry) Delete(deviceID string) bool {
	r.mu.Lock()
	record, found := r.devices[deviceID]
	delete(r.devices, deviceID)
	r.mu.Unlock()
	if found && record.cancel != nil {
		record.cancel()
	}
	if found && record.session != nil {
		_ = record.session.Close()
	}
	if found && record.mediaUnsubscribe != nil {
		record.mediaUnsubscribe()
	}
	return found
}

// Connect starts one bounded protocol attempt. Repeated requests while an
// attempt is active are idempotent and never open a second UDP transport.
func (r *Registry) Connect(deviceID string) (Device, bool, error) {
	r.mu.Lock()
	record, found := r.devices[deviceID]
	if !found {
		r.mu.Unlock()
		return Device{}, false, ErrDeviceNotFound
	}
	if record.connectActive {
		device := record.device
		r.mu.Unlock()
		return device, false, nil
	}
	if r.connector == nil {
		r.mu.Unlock()
		return Device{}, false, errors.New("PLAF203 connector is unavailable")
	}
	attemptContext, cancel := context.WithCancel(context.Background())
	record.cancel = cancel
	record.connectActive = true
	record.attemptID++
	attemptID := record.attemptID
	r.devices[deviceID] = record
	device := record.device
	uid := record.uid
	ip := append(net.IP(nil), record.ip...)
	connector := r.connector
	r.mu.Unlock()

	go r.runConnect(attemptContext, connector, deviceID, uid, ip, attemptID)
	return device, true, nil
}

// Disconnect cancels an active attempt and returns the device to the idle state.
func (r *Registry) Disconnect(deviceID string) bool {
	r.mu.Lock()
	record, found := r.devices[deviceID]
	if !found {
		r.mu.Unlock()
		return false
	}
	cancel := record.cancel
	session := record.session
	record.attemptID++
	record.connectActive = false
	record.cancel = nil
	record.session = nil
	mediaUnsubscribe := record.mediaUnsubscribe
	record.mediaUnsubscribe = nil
	record.device.ConnectionState = plaf203.StateIdle
	record.device.LastError = ""
	record.device.LastTransitionAt = time.Now().UTC()
	record.device.UpdatedAt = record.device.LastTransitionAt
	r.devices[deviceID] = record
	r.mu.Unlock()
	if mediaUnsubscribe != nil {
		mediaUnsubscribe()
	}
	if cancel != nil {
		cancel()
	}
	if session != nil {
		_ = session.Close()
	}
	return true
}

// List returns the safe state of every registered device in stable order.
func (r *Registry) List() []Device {
	r.mu.RLock()
	devices := make([]Device, 0, len(r.devices))
	for _, record := range r.devices {
		device := record.device
		if record.session != nil {
			stats := record.session.MediaStats()
			device.VideoCodec = stats.VideoCodec
			device.AudioCodec = stats.AudioCodec
			device.FramesReceived = stats.FramesReceived
			device.BytesReceived = stats.BytesReceived
			device.LastFrameAt = stats.LastFrameAt
		}
		devices = append(devices, device)
	}
	r.mu.RUnlock()
	sort.Slice(devices, func(left int, right int) bool {
		return devices[left].DeviceID < devices[right].DeviceID
	})
	return devices
}

func (r *Registry) runConnect(ctx context.Context, connector plaf203.Connector, deviceID string, uid string, ip net.IP, attemptID uint64) {
	session, err := connector.Connect(ctx, uid, ip, func(event plaf203.Event) {
		r.transition(deviceID, attemptID, event)
	})
	if err == nil {
		r.connected(deviceID, attemptID, session)
		return
	}
	r.fail(deviceID, attemptID, err)
}

func (r *Registry) connected(deviceID string, attemptID uint64, session *plaf203.Session) {
	r.mu.Lock()
	record, found := r.devices[deviceID]
	if !found || record.attemptID != attemptID {
		r.mu.Unlock()
		if session != nil {
			_ = session.Close()
		}
		return
	}
	record.session = session
	if record.media != nil && session != nil {
		record.mediaUnsubscribe = session.SubscribeFrames(record.media.publish)
	}
	record.cancel = nil
	record.device.LastError = ""
	r.devices[deviceID] = record
	r.mu.Unlock()
}

func (r *Registry) addMediaConsumer(deviceID string, consumer *rtsp.Conn) (func(), error) {
	_, _, connectErr := r.Connect(deviceID)
	if connectErr != nil {
		return nil, connectErr
	}
	r.mu.Lock()
	record, found := r.devices[deviceID]
	if !found || record.media == nil {
		r.mu.Unlock()
		return nil, ErrDeviceNotFound
	}
	if err := record.media.attach(consumer); err != nil {
		r.mu.Unlock()
		return nil, err
	}
	record.device.MediaConsumers++
	record.device.UpdatedAt = time.Now().UTC()
	r.devices[deviceID] = record
	r.mu.Unlock()
	var once sync.Once
	return func() {
		once.Do(func() {
			r.mu.Lock()
			record, found := r.devices[deviceID]
			if found && record.device.MediaConsumers > 0 {
				record.device.MediaConsumers--
				record.device.UpdatedAt = time.Now().UTC()
				r.devices[deviceID] = record
			}
			r.mu.Unlock()
			log.Printf("CAMERA MEDIA CLIENT DISCONNECTED device=%s", deviceID)
		})
	}, nil
}

func (r *Registry) transition(deviceID string, attemptID uint64, event plaf203.Event) {
	r.mu.Lock()
	record, found := r.devices[deviceID]
	if !found || record.attemptID != attemptID {
		r.mu.Unlock()
		return
	}
	now := time.Now().UTC()
	record.device.ConnectionState = event.State
	record.device.LastError = ""
	if event.State == plaf203.StateStreaming {
		record.device.StreamAvailable = true
		record.device.Reason = ""
	}
	record.device.LastTransitionAt = now
	record.device.UpdatedAt = now
	r.devices[deviceID] = record
	r.mu.Unlock()

	switch event.State {
	case plaf203.StateDiscovering:
		switch event.Step {
		case "route_source":
			log.Printf("CAMERA ROUTE SOURCE device=%s target=%s source=%s", deviceID, safeAddress(event.Address), safeAddress(event.LocalAddress))
		case "route_failed":
			log.Printf("CAMERA DISCOVERY ROUTE FAILED device=%s target=%s error=%s", deviceID, safeAddress(event.Address), event.Error)
		case "udp_socket":
			log.Printf("CAMERA UDP SOCKET device=%s local=%s", deviceID, safeAddress(event.LocalAddress))
		case "udp_tx":
			log.Printf("CAMERA UDP TX device=%s target=%s bytes=%d", deviceID, safeAddress(event.Address), event.PacketLength)
		case "udp_tx_ok":
			log.Printf("CAMERA UDP TX OK device=%s bytes=%d local=%s", deviceID, event.PacketLength, safeAddress(event.LocalAddress))
		case "udp_tx_failed":
			log.Printf("CAMERA UDP TX FAILED device=%s target=%s error=%s", deviceID, safeAddress(event.Address), event.Error)
		case "response":
			log.Printf("CAMERA DISCOVERY RESPONSE device=%s peer=%s bytes=%d opcode=0x%04x", deviceID, safeAddress(event.Address), event.PacketLength, event.Opcode)
		case "valid":
			log.Printf("CAMERA DISCOVERY VALID device=%s peer=%s bytes=%d opcode=0x%04x", deviceID, safeAddress(event.Address), event.PacketLength, event.Opcode)
		case "phase_two_tx":
			log.Printf("CAMERA DISCOVERY PHASE2 TX device=%s peer=%s bytes=%d", deviceID, safeAddress(event.Address), event.PacketLength)
		case "complete":
			log.Printf("CAMERA DISCOVERY COMPLETE device=%s peer=%s", deviceID, safeAddress(event.Address))
		case "reject_missing_peer", "reject_source_ip", "reject_uid_mismatch", "reject_invalid_length", "reject_invalid_packet":
			log.Printf("DEBUG CAMERA DISCOVERY REJECT device=%s peer=%s bytes=%d opcode=0x%04x reason=%s", deviceID, safeAddress(event.Address), event.PacketLength, event.Opcode, event.Step)
		case "unicast_timeout":
			log.Printf("CAMERA DISCOVERY UNICAST TIMEOUT device=%s", deviceID)
		case "broadcast_fallback":
			log.Printf("CAMERA DISCOVERY FALLBACK device=%s mode=broadcast", deviceID)
		case "broadcast":
			log.Printf("CAMERA DISCOVERY START device=%s mode=broadcast", deviceID)
		default:
			log.Printf("CAMERA DISCOVERY START device=%s mode=%s", deviceID, event.Step)
		}
	case plaf203.StateKnocking:
		if event.Step == "wait" {
			log.Printf("CAMERA KNOCK WAIT device=%s peer=%s", deviceID, safeAddress(event.Address))
			return
		}
		mode := event.Step
		if mode != "unicast" && mode != "broadcast" {
			mode = "broadcast"
		}
		log.Printf("CAMERA DISCOVERY FOUND device=%s mode=%s peer=%s", deviceID, mode, safeAddress(event.Address))
		log.Printf("CAMERA KNOCK START device=%s", deviceID)
	case plaf203.StateLoggingIn:
		if event.Step == "" {
			log.Printf("CAMERA LOGIN START device=%s", deviceID)
		} else {
			log.Printf("CAMERA LOGIN STEP device=%s step=%s", deviceID, event.Step)
		}
	case plaf203.StateConnected:
		log.Printf("CAMERA LOGIN OK device=%s", deviceID)
		log.Printf("CAMERA SESSION CONNECTED device=%s", deviceID)
	case plaf203.StateBootstrapping:
		switch event.Step {
		case "rx":
			log.Printf("CAMERA BOOTSTRAP RX device=%s peer=%s bytes=%d outer_opcode=0x%04x session_channel=0x%04x session_cmd=0x%04x control_type=0x%08x seq=%d", deviceID, safeAddress(event.Address), event.PacketLength, event.Opcode, event.SessionChannel, event.SessionCommand, event.ControlType, event.Sequence)
		case "ignore":
			log.Printf("CAMERA BOOTSTRAP IGNORE device=%s peer=%s bytes=%d outer_opcode=0x%04x session_channel=0x%04x session_cmd=0x%04x reason=%s", deviceID, safeAddress(event.Address), event.PacketLength, event.Opcode, event.SessionChannel, event.SessionCommand, event.Reason)
		case "timeout":
			log.Printf("CAMERA BOOTSTRAP TIMEOUT device=%s rx_packets=%d rx_bytes=%d types=%s rejected=%s", deviceID, event.PacketCount, event.ByteCount, event.Types, event.Rejected)
		case "stream_start_packet":
			log.Printf("CAMERA STREAM START PACKET device=%s bytes=%d outer_opcode=0x%04x channel=0x%04x session_cmd=0x%04x control_type=0x%04x seq=%d body_bytes=%d", deviceID, event.PacketLength, event.Opcode, event.SessionChannel, event.SessionCommand, event.ControlType, event.Sequence, event.BodyLength)
			log.Printf("DEBUG CAMERA STREAM START PACKET device=%s decoded_hex=%s wire_hex=%s", deviceID, event.DecodedHex, event.WireHex)
		case "keepalive_rx":
			log.Printf("CAMERA KEEPALIVE RX device=%s peer=%s bytes=%d opcode=0x%04x", deviceID, safeAddress(event.Address), event.PacketLength, event.Opcode)
		case "keepalive_tx":
			log.Printf("CAMERA KEEPALIVE TX device=%s peer=%s bytes=%d opcode=0x%04x", deviceID, safeAddress(event.Address), event.PacketLength, event.Opcode)
		case "session_heartbeat_rx":
			log.Printf("CAMERA SESSION HEARTBEAT RX device=%s peer=%s bytes=%d", deviceID, safeAddress(event.Address), event.PacketLength)
		case "session_heartbeat_tx":
			log.Printf("CAMERA SESSION HEARTBEAT TX device=%s peer=%s bytes=%d", deviceID, safeAddress(event.Address), event.PacketLength)
		case "session25_counters_rx":
			log.Printf("CAMERA SESSION25 COUNTERS RX device=%s peer=%s seq_send_cmd1=%d seq_send_cmd2=%d seq_recv_cmd2=%d seq_recv_pkt0=%d seq_recv_pkt1=%d seq_send_cnt=%d", deviceID, safeAddress(event.Address), event.Session25SeqSendCmd1, event.Session25SeqSendCmd2, event.Session25SeqRecvCmd2, event.Session25SeqRecvPkt0, event.Session25SeqRecvPkt1, event.Session25SeqSendCnt)
		case "session25_counters_tx":
			log.Printf("CAMERA SESSION25 COUNTERS TX device=%s peer=%s seq_send_cmd1=%d seq_send_cmd2=%d seq_recv_cmd2=%d seq_recv_pkt0=%d seq_recv_pkt1=%d seq_send_cnt=%d", deviceID, safeAddress(event.Address), event.Session25SeqSendCmd1, event.Session25SeqSendCmd2, event.Session25SeqRecvCmd2, event.Session25SeqRecvPkt0, event.Session25SeqRecvPkt1, event.Session25SeqSendCnt)
		case "stream_start_tx":
			log.Printf("CAMERA STREAM START TX device=%s channel=0x%04x control_type=0x%04x", deviceID, 0x7000, event.ControlType)
		case "stream_start_ack":
			log.Printf("CAMERA STREAM START ACK device=%s control_type=0x%04x", deviceID, event.ControlType)
		case "media_rx":
			log.Printf("CAMERA MEDIA RX device=%s peer=%s bytes=%d channel=0x%04x seq=%d", deviceID, safeAddress(event.Address), event.PacketLength, event.SessionChannel, event.Sequence)
		default:
			log.Printf("CAMERA BOOTSTRAP START device=%s", deviceID)
		}
	case plaf203.StateStreaming:
		if event.Step != "media_stats" {
			log.Printf("CAMERA BOOTSTRAP OK device=%s", deviceID)
			log.Printf("CAMERA STREAM START device=%s codec=h264", deviceID)
		}
		if event.Frame != nil {
			log.Printf("CAMERA VIDEO FRAME device=%s codec=%s keyframe=%t bytes=%d timestamp=%d", deviceID, event.Frame.Codec, event.Frame.Keyframe, len(event.Frame.Data), event.Frame.Timestamp)
		}
	}
}

func (r *Registry) fail(deviceID string, attemptID uint64, connectionError error) {
	r.mu.Lock()
	record, found := r.devices[deviceID]
	if !found || record.attemptID != attemptID {
		r.mu.Unlock()
		return
	}
	failedState := record.device.ConnectionState
	now := time.Now().UTC()
	record.connectActive = false
	record.cancel = nil
	record.device.ConnectionState = plaf203.StateFailed
	record.device.StreamAvailable = false
	record.device.Reason = "camera_session_failed"
	record.device.LastError = connectionError.Error()
	record.device.LastTransitionAt = now
	record.device.UpdatedAt = now
	r.devices[deviceID] = record
	r.mu.Unlock()
	if failedState == plaf203.StateLoggingIn {
		log.Printf("CAMERA LOGIN FAILED device=%s error=%v", deviceID, connectionError)
	} else if failedState == plaf203.StateBootstrapping {
		log.Printf("CAMERA BOOTSTRAP FAILED device=%s error=%v", deviceID, connectionError)
	} else {
		log.Printf("CAMERA SESSION FAILED device=%s stage=%s error=%v", deviceID, failedState, connectionError)
	}
}

func safeAddress(address *net.UDPAddr) string {
	if address == nil {
		return "unknown"
	}
	return address.String()
}

func validDeviceID(deviceID string) bool {
	if deviceID == "" {
		return false
	}
	for _, character := range deviceID {
		if (character < 'A' || character > 'Z') &&
			(character < 'a' || character > 'z') &&
			(character < '0' || character > '9') &&
			character != '_' && character != '-' {
			return false
		}
	}
	return true
}

func validUID(uid string) bool {
	if len(uid) != petlibroUIDLength {
		return false
	}
	return strings.IndexFunc(uid, func(character rune) bool {
		return character < 0x21 || character > 0x7e
	}) == -1
}

func parseIPv4(value string) (net.IP, error) {
	if value == "" {
		return nil, nil
	}
	parsed := net.ParseIP(value)
	if parsed == nil || parsed.To4() == nil {
		return nil, ErrInvalidIP
	}
	return append(net.IP(nil), parsed.To4()...), nil
}
