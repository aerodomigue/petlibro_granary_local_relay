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
