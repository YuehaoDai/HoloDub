package httpx

import "encoding/json"

type extracted struct {
	code    string
	message string
}

// extractCodeAndMessage is a defensive parser for the most common error
// response shapes used by OpenAI-compatible providers and our own ml-service:
//
//	{"error":"code","message":"..."}                   // ml-service / Go API
//	{"error":{"message":"...","code":"..."}}            // OpenAI / DeepSeek
//	{"error":{"message":"..."}}                          // Qwen / generic
//	{"detail":"..."}                                     // FastAPI defaults
//	{"detail":{"error":"...","message":"..."}}           // ml-service /tts/run new shape
//
// Unknown shapes return nil so the caller falls back to http.StatusText.
func extractCodeAndMessage(body []byte) *extracted {
	if len(body) == 0 {
		return nil
	}
	// Object form
	var obj map[string]any
	if err := json.Unmarshal(body, &obj); err != nil {
		return nil
	}

	out := &extracted{}

	// Look for `error`: may be string or nested object.
	if raw, ok := obj["error"]; ok {
		switch v := raw.(type) {
		case string:
			out.code = v
		case map[string]any:
			if c, ok := v["code"].(string); ok {
				out.code = c
			}
			if m, ok := v["message"].(string); ok {
				out.message = m
			}
		}
	}
	// Top-level message wins over nested if both present.
	if m, ok := obj["message"].(string); ok && m != "" {
		out.message = m
	}
	// FastAPI: `detail` may carry the structured error from our ml-service.
	if raw, ok := obj["detail"]; ok && out.message == "" {
		switch v := raw.(type) {
		case string:
			out.message = v
		case map[string]any:
			if m, ok := v["message"].(string); ok {
				out.message = m
			}
			if c, ok := v["error"].(string); ok && out.code == "" {
				out.code = c
			}
		}
	}

	if out.code == "" && out.message == "" {
		return nil
	}
	return out
}
