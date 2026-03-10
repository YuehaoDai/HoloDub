package http

import (
	"net/http"

	"github.com/gin-gonic/gin"
)

type errorResponse struct {
	Code      string `json:"code"`
	Message   string `json:"message"`
	RequestID string `json:"request_id,omitempty"`
}

func respondError(c *gin.Context, statusCode int, code, message string) {
	c.JSON(statusCode, errorResponse{
		Code:      code,
		Message:   message,
		RequestID: requestIDFromContext(c),
	})
}

func respondAccepted(c *gin.Context, payload any) {
	c.JSON(http.StatusAccepted, payload)
}
