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
	// ErrDeviceNotFound rejects an explicit connection request for an unregistered device.
	ErrDeviceNotFound = errors.New("device is not registered")
)

// Device is the non-sensitive state exposed by the bridge API.
// The UID remains internal to the bridge; callers only learn that it was registered.
type Device struct {
	DeviceID         string               `json:"device_id"`
	UIDLearned       bool                 `json:"uid_learned"`
	Stream           string               `json:"stream"`
	StreamAvailable  bool                 `json:"stream_available"`
	Reason           string               `json:"reason"`
	ConnectionState  plaf203.SessionState `json:"connection_state"`
	LastError        string               `json:"last_error,omitempty"`
	LastTransitionAt time.Time            `json:"last_transition_at"`
	UpdatedAt        time.Time            `json:"updated_at"`
}

type deviceRecord struct {
	device        Device
	uid           string
	cancel        context.CancelFunc
	attemptID     uint64
	connectActive bool
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
	return NewRegistryWithConnector(plaf203.NewDirectConnector())
}

// NewRegistryWithConnector allows protocol behavior to be isolated by fakes in tests.
func NewRegistryWithConnector(connector plaf203.Connector) *Registry {
	return &Registry{
		devices:   make(map[string]deviceRecord),
		connector: connector,
	}
}

// Upsert validates and records one UID. Repeating the same mapping is idempotent.
func (r *Registry) Upsert(deviceID string, uid string) (Device, error) {
	if !validDeviceID(deviceID) {
		return Device{}, ErrInvalidDeviceID
	}
	if !validUID(uid) {
		return Device{}, ErrInvalidUID
	}

	// Keep the official transport dependency compile-checked. A real session
	// must use this package only after the Petlibro-specific protocol has a
	// separate, tested implementation; it must not borrow fork code at runtime.
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
			Reason:           "plaf203_login_not_implemented",
			ConnectionState:  plaf203.StateIdle,
			LastTransitionAt: now,
		}
	}
	record.uid = uid
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
	connector := r.connector
	r.mu.Unlock()

	go r.runConnect(attemptContext, connector, deviceID, uid, attemptID)
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
	record.attemptID++
	record.connectActive = false
	record.cancel = nil
	record.device.ConnectionState = plaf203.StateIdle
	record.device.LastError = ""
	record.device.LastTransitionAt = time.Now().UTC()
	record.device.UpdatedAt = record.device.LastTransitionAt
	r.devices[deviceID] = record
	r.mu.Unlock()
	if cancel != nil {
		cancel()
	}
	return true
}

// List returns the safe state of every registered device in stable order.
func (r *Registry) List() []Device {
	r.mu.RLock()
	devices := make([]Device, 0, len(r.devices))
	for _, record := range r.devices {
		devices = append(devices, record.device)
	}
	r.mu.RUnlock()
	sort.Slice(devices, func(left int, right int) bool {
		return devices[left].DeviceID < devices[right].DeviceID
	})
	return devices
}

func (r *Registry) runConnect(ctx context.Context, connector plaf203.Connector, deviceID string, uid string, attemptID uint64) {
	err := connector.Connect(ctx, uid, func(event plaf203.Event) {
		r.transition(deviceID, attemptID, event)
	})
	if err == nil {
		r.transition(deviceID, attemptID, plaf203.Event{State: plaf203.StateConnected})
		return
	}
	r.fail(deviceID, attemptID, err)
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
	record.device.LastTransitionAt = now
	record.device.UpdatedAt = now
	r.devices[deviceID] = record
	r.mu.Unlock()

	switch event.State {
	case plaf203.StateDiscovering:
		log.Printf("CAMERA DISCOVERY START device=%s", deviceID)
	case plaf203.StateKnocking:
		log.Printf("CAMERA DISCOVERY FOUND device=%s addr=%s", deviceID, safeAddress(event.Address))
		log.Printf("CAMERA KNOCK START device=%s", deviceID)
	case plaf203.StateLoggingIn:
		log.Printf("CAMERA KNOCK OK device=%s", deviceID)
		log.Printf("CAMERA LOGIN START device=%s", deviceID)
	case plaf203.StateConnected:
		log.Printf("CAMERA SESSION CONNECTED device=%s", deviceID)
	}
}

func (r *Registry) fail(deviceID string, attemptID uint64, connectionError error) {
	r.mu.Lock()
	record, found := r.devices[deviceID]
	if !found || record.attemptID != attemptID {
		r.mu.Unlock()
		return
	}
	now := time.Now().UTC()
	record.connectActive = false
	record.cancel = nil
	record.device.ConnectionState = plaf203.StateFailed
	record.device.LastError = connectionError.Error()
	record.device.LastTransitionAt = now
	record.device.UpdatedAt = now
	r.devices[deviceID] = record
	r.mu.Unlock()
	log.Printf("CAMERA SESSION FAILED device=%s stage=%s error=%v", deviceID, connectionStage(connectionError), connectionError)
}

func connectionStage(connectionError error) string {
	if errors.Is(connectionError, plaf203.ErrLoginUnsupported) {
		return "login"
	}
	return "discovery_or_knock"
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
