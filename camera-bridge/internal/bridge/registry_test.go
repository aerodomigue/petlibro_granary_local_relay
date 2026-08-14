package bridge

import (
	"context"
	"errors"
	"net"
	"sync"
	"testing"
	"time"

	"github.com/aerodomigue/petlibro-camera-bridge/internal/plaf203"
)

func TestRegistryConnectionLifecycleIsSingleAttemptAndRetryable(t *testing.T) {
	connector := &scriptedConnector{release: make(chan struct{})}
	registry := NewRegistryWithConnector(connector)
	if _, err := registry.Upsert(testDeviceID, testUID, "192.0.2.10"); err != nil {
		t.Fatal(err)
	}

	if _, started, err := registry.Connect(testDeviceID); err != nil || !started {
		t.Fatalf("first connect started=%t err=%v", started, err)
	}
	if _, started, err := registry.Connect(testDeviceID); err != nil || started {
		t.Fatalf("second connect started=%t err=%v", started, err)
	}
	awaitState(t, registry, plaf203.StateDiscovering)
	close(connector.release)
	awaitState(t, registry, plaf203.StateFailed)
	if calls := connector.CallCount(); calls != 1 {
		t.Fatalf("calls=%d", calls)
	}

	connector.reset()
	if _, started, err := registry.Connect(testDeviceID); err != nil || !started {
		t.Fatalf("retry started=%t err=%v", started, err)
	}
	awaitState(t, registry, plaf203.StateDiscovering)
	registry.Disconnect(testDeviceID)
	awaitState(t, registry, plaf203.StateIdle)
}

func TestRegistryDevicesConnectIndependently(t *testing.T) {
	connector := &scriptedConnector{release: make(chan struct{})}
	registry := NewRegistryWithConnector(connector)
	if _, err := registry.Upsert("DEVICE_A", testUID, "192.0.2.10"); err != nil {
		t.Fatal(err)
	}
	if _, err := registry.Upsert("DEVICE_B", "PLAF2030000000000002", "192.0.2.11"); err != nil {
		t.Fatal(err)
	}
	if _, started, err := registry.Connect("DEVICE_A"); err != nil || !started {
		t.Fatalf("A started=%t err=%v", started, err)
	}
	if _, started, err := registry.Connect("DEVICE_B"); err != nil || !started {
		t.Fatalf("B started=%t err=%v", started, err)
	}
	awaitCalls(t, connector, 2)
	addresses := connector.IPs()
	if len(addresses) != 2 || addresses[0] == addresses[1] {
		t.Fatalf("connector received unexpected device addresses: %v", addresses)
	}
	registry.Disconnect("DEVICE_A")
	registry.Disconnect("DEVICE_B")
}

func TestRegistryUpdatesKnownIPWithoutInterruptingAnIdleDevice(t *testing.T) {
	connector := &scriptedConnector{release: make(chan struct{})}
	registry := NewRegistryWithConnector(connector)
	if _, err := registry.Upsert(testDeviceID, testUID, "192.0.2.10"); err != nil {
		t.Fatal(err)
	}
	if _, err := registry.Upsert(testDeviceID, testUID, "192.0.2.11"); err != nil {
		t.Fatal(err)
	}
	devices := registry.List()
	if len(devices) != 1 || devices[0].IP != "192.0.2.11" || devices[0].ConnectionState != plaf203.StateIdle {
		t.Fatalf("device update=%+v", devices)
	}
	if _, started, err := registry.Connect(testDeviceID); err != nil || !started {
		t.Fatalf("connect started=%t err=%v", started, err)
	}
	awaitState(t, registry, plaf203.StateDiscovering)
	addresses := connector.IPs()
	if len(addresses) != 1 || addresses[0] != "192.0.2.11" {
		t.Fatalf("connector received addresses=%v", addresses)
	}
	registry.Disconnect(testDeviceID)
}

type callCounter interface {
	CallCount() int
}

func awaitCalls(t *testing.T, connector callCounter, want int) {
	t.Helper()
	deadline := time.Now().Add(time.Second)
	for time.Now().Before(deadline) {
		if connector.CallCount() == want {
			return
		}
		time.Sleep(time.Millisecond)
	}
	t.Fatalf("calls=%d want=%d", connector.CallCount(), want)
}

func awaitState(t *testing.T, registry *Registry, want plaf203.SessionState) {
	t.Helper()
	deadline := time.Now().Add(time.Second)
	for time.Now().Before(deadline) {
		for _, device := range registry.List() {
			if device.DeviceID == testDeviceID && device.ConnectionState == want {
				return
			}
		}
		time.Sleep(time.Millisecond)
	}
	t.Fatalf("state did not become %s: %+v", want, registry.List())
}

func awaitDeviceStateAndConsumers(t *testing.T, registry *Registry, state plaf203.SessionState, consumers int) {
	t.Helper()
	deadline := time.Now().Add(time.Second)
	for time.Now().Before(deadline) {
		for _, device := range registry.List() {
			if device.DeviceID == testDeviceID && device.ConnectionState == state && device.MediaConsumers == consumers {
				return
			}
		}
		time.Sleep(time.Millisecond)
	}
	t.Fatalf("state/consumers did not become %s/%d: %+v", state, consumers, registry.List())
}

func assertDeviceStateAndConsumers(t *testing.T, registry *Registry, state plaf203.SessionState, consumers int) {
	t.Helper()
	for _, device := range registry.List() {
		if device.DeviceID == testDeviceID {
			if device.ConnectionState != state || device.MediaConsumers != consumers {
				t.Fatalf("state=%s consumers=%d want=%s/%d", device.ConnectionState, device.MediaConsumers, state, consumers)
			}
			return
		}
	}
	t.Fatalf("device missing: %+v", registry.List())
}

type scriptedConnector struct {
	mu      sync.Mutex
	calls   int
	ips     []string
	release chan struct{}
}

func (connector *scriptedConnector) Connect(ctx context.Context, uid string, ip net.IP, observer plaf203.Observer) (*plaf203.Session, error) {
	connector.mu.Lock()
	connector.calls++
	connector.ips = append(connector.ips, ip.String())
	release := connector.release
	connector.mu.Unlock()
	observer(plaf203.Event{State: plaf203.StateDiscovering})
	select {
	case <-release:
		observer(plaf203.Event{State: plaf203.StateKnocking})
		observer(plaf203.Event{State: plaf203.StateLoggingIn})
		return nil, errors.New("test login failed")
	case <-ctx.Done():
		return nil, ctx.Err()
	}
}

func (connector *scriptedConnector) IPs() []string {
	connector.mu.Lock()
	defer connector.mu.Unlock()
	return append([]string(nil), connector.ips...)
}

func (connector *scriptedConnector) CallCount() int {
	connector.mu.Lock()
	defer connector.mu.Unlock()
	return connector.calls
}

func (connector *scriptedConnector) reset() {
	connector.mu.Lock()
	connector.release = make(chan struct{})
	connector.mu.Unlock()
}

func TestRegistryRejectsUnknownDeviceConnection(t *testing.T) {
	registry := NewRegistryWithConnector(&scriptedConnector{release: make(chan struct{})})
	if _, _, err := registry.Connect("UNKNOWN"); !errors.Is(err, ErrDeviceNotFound) {
		t.Fatalf("error=%v", err)
	}
}

func TestRegistryCanRepresentAConfirmedFutureLoginWithoutFalseProductionSuccess(t *testing.T) {
	registry := NewRegistryWithConnector(connectedConnector{})
	if _, err := registry.Upsert(testDeviceID, testUID, ""); err != nil {
		t.Fatal(err)
	}
	if _, started, err := registry.Connect(testDeviceID); err != nil || !started {
		t.Fatalf("started=%t err=%v", started, err)
	}
	awaitState(t, registry, plaf203.StateConnected)
}

func TestRegistryKeepsFeederSessionUntilTheLastMediaConsumerLeaves(t *testing.T) {
	registry := NewRegistryWithConnectorAndIdleTimeout(connectedConnector{}, 20*time.Millisecond)
	if _, err := registry.Upsert(testDeviceID, testUID, "192.0.2.10"); err != nil {
		t.Fatal(err)
	}
	if _, started, err := registry.Connect(testDeviceID); err != nil || !started {
		t.Fatalf("connect started=%t err=%v", started, err)
	}
	awaitState(t, registry, plaf203.StateConnected)

	registry.mu.Lock()
	record := registry.devices[testDeviceID]
	record.device.MediaConsumers = 2
	record.device.ConnectionState = plaf203.StateStreaming
	record.device.StreamAvailable = true
	registry.devices[testDeviceID] = record
	registry.mu.Unlock()

	registry.releaseMediaConsumer(testDeviceID)
	time.Sleep(40 * time.Millisecond)
	assertDeviceStateAndConsumers(t, registry, plaf203.StateStreaming, 1)

	registry.releaseMediaConsumer(testDeviceID)
	awaitDeviceStateAndConsumers(t, registry, plaf203.StateIdle, 0)
}

func TestRegistryCancelsIdleTeardownWhenANewConsumerArrives(t *testing.T) {
	registry := NewRegistryWithConnectorAndIdleTimeout(connectedConnector{}, 50*time.Millisecond)
	if _, err := registry.Upsert(testDeviceID, testUID, "192.0.2.10"); err != nil {
		t.Fatal(err)
	}

	registry.mu.Lock()
	record := registry.devices[testDeviceID]
	record.device.MediaConsumers = 1
	record.device.ConnectionState = plaf203.StateStreaming
	record.device.StreamAvailable = true
	registry.devices[testDeviceID] = record
	registry.mu.Unlock()

	registry.releaseMediaConsumer(testDeviceID)
	registry.cancelIdleDisconnect(testDeviceID)
	registry.mu.Lock()
	record = registry.devices[testDeviceID]
	record.device.MediaConsumers = 1
	registry.devices[testDeviceID] = record
	registry.mu.Unlock()

	time.Sleep(80 * time.Millisecond)
	assertDeviceStateAndConsumers(t, registry, plaf203.StateStreaming, 1)
}

func TestRegistryReconnectsOnlyWhenAConsumedSessionIsLost(t *testing.T) {
	connector := &streamingConnector{}
	registry := NewRegistryWithConnectorAndIdleTimeout(connector, time.Second)
	registry.reconnectDelay = func(uint) time.Duration { return time.Millisecond }
	if _, err := registry.Upsert(testDeviceID, testUID, "192.0.2.10"); err != nil {
		t.Fatal(err)
	}
	if _, started, err := registry.Connect(testDeviceID); err != nil || !started {
		t.Fatalf("connect started=%t err=%v", started, err)
	}
	awaitCalls(t, connector, 1)

	registry.mu.Lock()
	record := registry.devices[testDeviceID]
	record.device.MediaConsumers = 1
	attemptID := record.attemptID
	registry.devices[testDeviceID] = record
	registry.mu.Unlock()
	registry.transition(testDeviceID, attemptID, plaf203.Event{State: plaf203.StateFailed, Step: "media_receive_failed", Error: "test transport closed"})
	awaitCalls(t, connector, 2)
}

type connectedConnector struct{}

func (connectedConnector) Connect(_ context.Context, _ string, _ net.IP, observer plaf203.Observer) (*plaf203.Session, error) {
	observer(plaf203.Event{State: plaf203.StateDiscovering})
	observer(plaf203.Event{State: plaf203.StateKnocking})
	observer(plaf203.Event{State: plaf203.StateLoggingIn})
	observer(plaf203.Event{State: plaf203.StateConnected})
	return &plaf203.Session{}, nil
}

type streamingConnector struct {
	mu    sync.Mutex
	calls int
}

func (connector *streamingConnector) Connect(_ context.Context, _ string, _ net.IP, observer plaf203.Observer) (*plaf203.Session, error) {
	connector.mu.Lock()
	connector.calls++
	connector.mu.Unlock()
	observer(plaf203.Event{State: plaf203.StateDiscovering})
	observer(plaf203.Event{State: plaf203.StateConnected})
	observer(plaf203.Event{State: plaf203.StateStreaming})
	return &plaf203.Session{}, nil
}

func (connector *streamingConnector) CallCount() int {
	connector.mu.Lock()
	defer connector.mu.Unlock()
	return connector.calls
}
