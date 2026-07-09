package plugin

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"net/url"
	"strings"
)

type appSettings struct {
	AssistantAPIURL string `json:"assistantApiUrl,omitempty"`
	AssistantWSURL  string `json:"assistantWsUrl,omitempty"`
	LegacyAPIURL    string `json:"apiUrl,omitempty"`
}

func (s *appSettings) normalize() {
	s.AssistantAPIURL = strings.TrimRight(strings.TrimSpace(s.AssistantAPIURL), "/")
	s.AssistantWSURL = strings.TrimSpace(s.AssistantWSURL)
	s.LegacyAPIURL = strings.TrimRight(strings.TrimSpace(s.LegacyAPIURL), "/")
	if s.AssistantAPIURL == "" {
		s.AssistantAPIURL = s.LegacyAPIURL
	}
}

type assistantSettingsResponse struct {
	AssistantAPIURL  string `json:"assistantApiUrl"`
	AssistantWSURL   string `json:"assistantWsUrl"`
	APIKeyConfigured bool   `json:"apiKeyConfigured"`
}

func (a *App) assistantBaseURL() string {
	return a.settings.AssistantAPIURL
}

func (a *App) handleAssistantSettings(w http.ResponseWriter, req *http.Request) {
	if req.Method != http.MethodGet {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}

	writeJSON(w, http.StatusOK, assistantSettingsResponse{
		AssistantAPIURL:  a.settings.AssistantAPIURL,
		AssistantWSURL:   a.settings.AssistantWSURL,
		APIKeyConfigured: a.apiKey != "",
	})
}

func (a *App) handleAssistantHealth(w http.ResponseWriter, req *http.Request) {
	if req.Method != http.MethodGet {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	a.proxyAssistant(w, req, http.MethodGet, "/health")
}

func (a *App) handleAssistantChat(w http.ResponseWriter, req *http.Request) {
	if req.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	a.proxyAssistant(w, req, http.MethodPost, "/api/assistant/chat")
}

func (a *App) proxyAssistant(w http.ResponseWriter, req *http.Request, method string, path string) {
	baseURL := a.assistantBaseURL()
	if baseURL == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{
			"error": "assistantApiUrl is not configured",
		})
		return
	}

	target, err := url.JoinPath(baseURL, path)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}

	var body io.Reader
	if req.Body != nil {
		defer req.Body.Close()
		raw, err := io.ReadAll(req.Body)
		if err != nil {
			writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
			return
		}
		body = bytes.NewReader(raw)
	}

	proxyReq, err := http.NewRequestWithContext(req.Context(), method, target, body)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	proxyReq.Header.Set("Content-Type", "application/json")
	if a.apiKey != "" {
		proxyReq.Header.Set("Authorization", "Bearer "+a.apiKey)
	}
	if grafanaUser := req.Header.Get("X-Grafana-User"); grafanaUser != "" {
		proxyReq.Header.Set("X-Grafana-User", grafanaUser)
	}

	resp, err := a.httpClient.Do(proxyReq)
	if err != nil {
		writeJSON(w, http.StatusBadGateway, map[string]string{"error": err.Error()})
		return
	}
	defer resp.Body.Close()

	w.Header().Set("Content-Type", resp.Header.Get("Content-Type"))
	w.WriteHeader(resp.StatusCode)
	if _, err := io.Copy(w, resp.Body); err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
	}
}

func writeJSON(w http.ResponseWriter, status int, payload any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	if err := json.NewEncoder(w).Encode(payload); err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
	}
}

// registerRoutes takes a *http.ServeMux and registers HTTP resource handlers.
func (a *App) registerRoutes(mux *http.ServeMux) {
	mux.HandleFunc("/assistant/settings", a.handleAssistantSettings)
	mux.HandleFunc("/assistant/health", a.handleAssistantHealth)
	mux.HandleFunc("/assistant/chat", a.handleAssistantChat)
}
