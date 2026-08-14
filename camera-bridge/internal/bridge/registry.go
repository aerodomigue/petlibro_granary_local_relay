// Package bridge implements the local, auditable registration API for cameras.
package bridge

import (
	"errors"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/AlexxIT/go2rtc/pkg/tutk"
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
)

// Device is the non-sensitive state exposed by the bridge API.
// The UID remains internal to the bridge; callers only learn that it was registered.
type Device struct {
	DeviceID        string    `json:"device_id"`
	UIDLearned      bool      `json:"uid_learned"`
	Stream          string    `json:"stream"`
	StreamAvailable bool      `json:"stream_available"`
	Reason          string    `json:"reason"`
	UpdatedAt       time.Time `json:"updated_at"`
}

type deviceRecord struct {
	device Device
	uid    string
}

// Registry keeps device registrations isolated by device ID.
// It intentionally does not start a TUTK session: the official package exposes
// the transport primitive, but not the Petlibro LAN handshake and AV protocol.
type Registry struct {
	mu      sync.RWMutex
	devices map[string]deviceRecord
}

// NewRegistry creates an empty in-memory registry. Durable UID storage is owned
// by the relay State Shadow; it re-registers devices after restart.
func NewRegistry() *Registry {
	return &Registry{devices: make(map[string]deviceRecord)}
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

	device := Device{
		DeviceID:        deviceID,
		UIDLearned:      true,
		Stream:          streamPrefix + deviceID,
		StreamAvailable: false,
		Reason:          "petlibro_protocol_not_implemented",
		UpdatedAt:       time.Now().UTC(),
	}
	r.mu.Lock()
	r.devices[deviceID] = deviceRecord{device: device, uid: uid}
	r.mu.Unlock()
	return device, nil
}

// Delete removes one device registration. It is idempotent and never contacts a camera.
func (r *Registry) Delete(deviceID string) bool {
	r.mu.Lock()
	_, found := r.devices[deviceID]
	delete(r.devices, deviceID)
	r.mu.Unlock()
	return found
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
