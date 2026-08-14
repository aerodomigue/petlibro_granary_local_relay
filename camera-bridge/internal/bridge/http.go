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
	mux.HandleFunc("DELETE /devices/{device_id}", func(writer http.ResponseWriter, request *http.Request) {
		registry.Delete(request.PathValue("device_id"))
		writer.WriteHeader(http.StatusNoContent)
	})
	return mux
}

func healthHandler(writer http.ResponseWriter, request *http.Request) {
	writeJSON(writer, http.StatusOK, map[string]any{
		"healthy":          true,
		"media_protocol":   "not_implemented",
		"tutk_dependency":  "github.com/AlexxIT/go2rtc/pkg/tutk",
		"stream_on_demand": false,
	})
}

type upsertRequest struct {
	UID string `json:"uid"`
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
	device, err := registry.Upsert(request.PathValue("device_id"), input.UID)
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
