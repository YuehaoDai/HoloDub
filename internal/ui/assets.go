package ui

import (
	"embed"
	"io/fs"
)

//go:embed all:static
var assets embed.FS

func FS() fs.FS {
	sub, err := fs.Sub(assets, "static")
	if err != nil {
		panic(err)
	}
	return sub
}
