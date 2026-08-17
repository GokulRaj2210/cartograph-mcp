// Package svc holds the Go side of the fixture: structs, methods, embedding.
package svc

import "strings"

// Record is a stored row.
type Record struct {
	ID   string
	Body string
}

// Reader is the read half of the store contract.
type Reader interface {
	Get(id string) Record
}

// Store keeps records in memory.
type Store struct {
	items map[string]Record
}

// Get returns a record by id.
func (s *Store) Get(id string) Record {
	return s.items[id]
}

// Put normalises the body and stores it.
func (s *Store) Put(id string, body string) {
	s.items[id] = Record{ID: id, Body: Normalize(body)}
}

// Normalize trims and lowercases a body.
func Normalize(body string) string {
	return strings.ToLower(strings.TrimSpace(body))
}
