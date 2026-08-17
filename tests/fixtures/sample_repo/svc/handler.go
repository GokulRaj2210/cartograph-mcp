package svc

// Handler serves requests out of a Store.
type Handler struct {
	store *Store
}

// Handle looks a record up and returns its body.
func (h *Handler) Handle(id string) string {
	rec := h.store.Get(id)
	return Normalize(rec.Body)
}
