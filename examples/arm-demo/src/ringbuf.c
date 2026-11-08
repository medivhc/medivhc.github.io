/* Freestanding ring buffer for a sensor ISR on an ARM SoC (no libc needed). */
#include "ringbuf.h"
void rb_init(ringbuf_t *rb) { rb->head = rb->tail = rb->count = 0; }
int rb_push(ringbuf_t *rb, uint32_t v) {
    if (rb->count == RB_CAP) return -1;
    rb->buf[rb->head] = v; rb->head = (rb->head + 1u) % RB_CAP; rb->count++; return 0;
}
int rb_pop(ringbuf_t *rb, uint32_t *out) {
    if (rb->count == 0) return -1;
    *out = rb->buf[rb->tail]; rb->tail = (rb->tail + 1u) % RB_CAP; rb->count--; return 0;
}
