package bridge

import (
	"context"
	"errors"
	"sync"
	"testing"
	"time"

	"github.com/aerodomigue/petlibro-camera-bridge/internal/plaf203"
)

func TestRegistryConnectionLifecycleIsSingleAttemptAndRetryable(t *testing.T) {
	connector := &scriptedConnector{release: make(chan struct{})}
	registry := NewRegistryWithConnector(connector)
	if _, err := registry.Upsert(testDeviceID, testUID); err != nil {
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
	if _, err := registry.Upsert("DEVICE_A", testUID); err != nil {
		t.Fatal(err)
	}
	if _, err := registry.Upsert("DEVICE_B", "PLAF2030000000000002"); err != nil {
		t.Fatal(err)
	}
	if _, started, err := registry.Connect("DEVICE_A"); err != nil || !started {
		t.Fatalf("A started=%t err=%v", started, err)
	}
	if _, started, err := registry.Connect("DEVICE_B"); err != nil || !started {
		t.Fatalf("B started=%t err=%v", started, err)
	}
	awaitCalls(t, connector, 2)
	registry.Disconnect("DEVICE_A")
	registry.Disconnect("DEVICE_B")
}

func awaitCalls(t *testing.T, connector *scriptedConnector, want int) {
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

type scriptedConnector struct {
	mu      sync.Mutex
	calls   int
	release chan struct{}
}

func (connector *scriptedConnector) Connect(ctx context.Context, uid string, observer plaf203.Observer) (*plaf203.Session, error) {
	connector.mu.Lock()
	connector.calls++
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
	if _, err := registry.Upsert(testDeviceID, testUID); err != nil {
		t.Fatal(err)
	}
	if _, started, err := registry.Connect(testDeviceID); err != nil || !started {
		t.Fatalf("started=%t err=%v", started, err)
	}
	awaitState(t, registry, plaf203.StateConnected)
}

type connectedConnector struct{}

func (connectedConnector) Connect(_ context.Context, _ string, observer plaf203.Observer) (*plaf203.Session, error) {
	observer(plaf203.Event{State: plaf203.StateDiscovering})
	observer(plaf203.Event{State: plaf203.StateKnocking})
	observer(plaf203.Event{State: plaf203.StateLoggingIn})
	observer(plaf203.Event{State: plaf203.StateConnected})
	return &plaf203.Session{}, nil
}
