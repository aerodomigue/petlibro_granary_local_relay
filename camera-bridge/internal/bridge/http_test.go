package bridge

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

const testDeviceID = "AF03040302A2B5B2CD60"
const testUID = "PLAF2030000000000001"

func TestDeviceRegistrationIsIdempotentAndDoesNotExposeUID(t *testing.T) {
	handler := NewHandler(NewRegistry())
	body := []byte(`{"uid":"` + testUID + `"}`)

	for attempt := 0; attempt < 2; attempt++ {
		request := httptest.NewRequest(http.MethodPut, "/devices/"+testDeviceID, bytes.NewReader(body))
		response := httptest.NewRecorder()
		handler.ServeHTTP(response, request)
		if response.Code != http.StatusOK {
			t.Fatalf("attempt %d: status=%d body=%s", attempt, response.Code, response.Body.String())
		}
		if bytes.Contains(response.Body.Bytes(), []byte(testUID)) {
			t.Fatal("UID leaked in registration response")
		}
	}

	request := httptest.NewRequest(http.MethodGet, "/devices", nil)
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, request)
	if response.Code != http.StatusOK || bytes.Contains(response.Body.Bytes(), []byte(testUID)) {
		t.Fatalf("unsafe device listing: status=%d body=%s", response.Code, response.Body.String())
	}
	var listing struct {
		Devices []Device `json:"devices"`
	}
	if err := json.Unmarshal(response.Body.Bytes(), &listing); err != nil {
		t.Fatal(err)
	}
	if len(listing.Devices) != 1 || !listing.Devices[0].UIDLearned || listing.Devices[0].StreamAvailable {
		t.Fatalf("unexpected listing: %+v", listing.Devices)
	}
}

func TestRegistrationRejectsInvalidInput(t *testing.T) {
	handler := NewHandler(NewRegistry())
	cases := []struct {
		name string
		path string
		body string
	}{
		{"short uid", "/devices/DEVICE_A", `{"uid":"short"}`},
		{"unknown field", "/devices/DEVICE_A", `{"uid":"PLAF2030000000000001","other":true}`},
		{"unsafe device id", "/devices/device!", `{"uid":"PLAF2030000000000001"}`},
	}
	for _, testCase := range cases {
		t.Run(testCase.name, func(t *testing.T) {
			request := httptest.NewRequest(http.MethodPut, testCase.path, strings.NewReader(testCase.body))
			response := httptest.NewRecorder()
			handler.ServeHTTP(response, request)
			if response.Code != http.StatusUnprocessableEntity && response.Code != http.StatusBadRequest {
				t.Fatalf("status=%d body=%s", response.Code, response.Body.String())
			}
		})
	}
}

func TestDeleteIsIdempotentAndHealthDoesNotClaimMediaIsAvailable(t *testing.T) {
	handler := NewHandler(NewRegistry())
	request := httptest.NewRequest(http.MethodGet, "/healthz", nil)
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, request)
	if response.Code != http.StatusOK || !bytes.Contains(response.Body.Bytes(), []byte(`"media_protocol":"not_implemented"`)) {
		t.Fatalf("unsafe health response: status=%d body=%s", response.Code, response.Body.String())
	}

	for attempt := 0; attempt < 2; attempt++ {
		request = httptest.NewRequest(http.MethodDelete, "/devices/"+testDeviceID, nil)
		response = httptest.NewRecorder()
		handler.ServeHTTP(response, request)
		if response.Code != http.StatusNoContent {
			t.Fatalf("attempt %d: status=%d", attempt, response.Code)
		}
	}
}

func TestConnectionEndpointsAreInternalAndIdempotent(t *testing.T) {
	connector := &scriptedConnector{release: make(chan struct{})}
	registry := NewRegistryWithConnector(connector)
	handler := NewHandler(registry)

	missing := httptest.NewRequest(http.MethodPost, "/devices/MISSING/connect", nil)
	missingResponse := httptest.NewRecorder()
	handler.ServeHTTP(missingResponse, missing)
	if missingResponse.Code != http.StatusNotFound {
		t.Fatalf("missing device status=%d", missingResponse.Code)
	}

	register := httptest.NewRequest(http.MethodPut, "/devices/"+testDeviceID, strings.NewReader(`{"uid":"`+testUID+`"}`))
	registerResponse := httptest.NewRecorder()
	handler.ServeHTTP(registerResponse, register)
	if registerResponse.Code != http.StatusOK {
		t.Fatalf("register status=%d", registerResponse.Code)
	}

	connect := httptest.NewRequest(http.MethodPost, "/devices/"+testDeviceID+"/connect", nil)
	connectResponse := httptest.NewRecorder()
	handler.ServeHTTP(connectResponse, connect)
	if connectResponse.Code != http.StatusAccepted || bytes.Contains(connectResponse.Body.Bytes(), []byte(testUID)) {
		t.Fatalf("connect status=%d body=%s", connectResponse.Code, connectResponse.Body.String())
	}
	secondConnect := httptest.NewRecorder()
	handler.ServeHTTP(secondConnect, httptest.NewRequest(http.MethodPost, "/devices/"+testDeviceID+"/connect", nil))
	if secondConnect.Code != http.StatusOK {
		t.Fatalf("idempotent connect status=%d", secondConnect.Code)
	}

	disconnect := httptest.NewRecorder()
	handler.ServeHTTP(disconnect, httptest.NewRequest(http.MethodPost, "/devices/"+testDeviceID+"/disconnect", nil))
	if disconnect.Code != http.StatusNoContent {
		t.Fatalf("disconnect status=%d", disconnect.Code)
	}
}
