package bridge

import (
	"encoding/json"
	"errors"
	"io"
	"net/http"
)

// NewHandler exposes only the internal camera registration API.
func NewHandler(registry *Registry) http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", healthHandler)
	mux.HandleFunc("GET /devices", func(writer http.ResponseWriter, request *http.Request) {
		writeJSON(writer, http.StatusOK, map[string]any{
			"devices": registry.List(),
		})
	})
	mux.HandleFunc("PUT /devices/{device_id}", func(writer http.ResponseWriter, request *http.Request) {
		upsertDevice(writer, request, registry)
	})
	mux.HandleFunc("POST /devices/{device_id}/connect", func(writer http.ResponseWriter, request *http.Request) {
		connectDevice(writer, request, registry)
	})
	mux.HandleFunc("POST /devices/{device_id}/disconnect", func(writer http.ResponseWriter, request *http.Request) {
		disconnectDevice(writer, request, registry)
	})
	mux.HandleFunc("DELETE /devices/{device_id}", func(writer http.ResponseWriter, request *http.Request) {
		registry.Delete(request.PathValue("device_id"))
		writer.WriteHeader(http.StatusNoContent)
	})
	return mux
}

func connectDevice(writer http.ResponseWriter, request *http.Request, registry *Registry) {
	device, started, err := registry.Connect(request.PathValue("device_id"))
	if errors.Is(err, ErrDeviceNotFound) {
		writeError(writer, http.StatusNotFound, "device is not registered")
		return
	}
	if err != nil {
		writeError(writer, http.StatusServiceUnavailable, "camera connection is unavailable")
		return
	}
	status := http.StatusOK
	if started {
		status = http.StatusAccepted
	}
	writeJSON(writer, status, device)
}

func disconnectDevice(writer http.ResponseWriter, request *http.Request, registry *Registry) {
	registry.Disconnect(request.PathValue("device_id"))
	writer.WriteHeader(http.StatusNoContent)
}

func healthHandler(writer http.ResponseWriter, request *http.Request) {
	writeJSON(writer, http.StatusOK, map[string]any{
		"healthy":          true,
		"media_protocol":   "plaf203_h264_observation",
		"tutk_dependency":  "github.com/AlexxIT/go2rtc/pkg/tutk",
		"stream_on_demand": true,
	})
}

type upsertRequest struct {
	UID string `json:"uid"`
	IP  string `json:"ip"`
}

func upsertDevice(writer http.ResponseWriter, request *http.Request, registry *Registry) {
	decoder := json.NewDecoder(http.MaxBytesReader(writer, request.Body, 1024))
	decoder.DisallowUnknownFields()
	var input upsertRequest
	if err := decoder.Decode(&input); err != nil {
		writeError(writer, http.StatusBadRequest, "invalid JSON payload")
		return
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		writeError(writer, http.StatusBadRequest, "invalid JSON payload")
		return
	}
	device, err := registry.Upsert(request.PathValue("device_id"), input.UID, input.IP)
	if errors.Is(err, ErrInvalidIP) {
		writeError(writer, http.StatusBadRequest, err.Error())
		return
	}
	if errors.Is(err, ErrInvalidDeviceID) || errors.Is(err, ErrInvalidUID) {
		writeError(writer, http.StatusUnprocessableEntity, err.Error())
		return
	}
	if err != nil {
		writeError(writer, http.StatusInternalServerError, "unable to register device")
		return
	}
	writeJSON(writer, http.StatusOK, device)
}

func writeError(writer http.ResponseWriter, status int, message string) {
	writeJSON(writer, status, map[string]string{"error": message})
}

func writeJSON(writer http.ResponseWriter, status int, payload any) {
	writer.Header().Set("Content-Type", "application/json; charset=utf-8")
	writer.WriteHeader(status)
	_ = json.NewEncoder(writer).Encode(payload)
}
