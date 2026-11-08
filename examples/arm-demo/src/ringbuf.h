#ifndef RINGBUF_H
#define RINGBUF_H
#include <stdint.h>
#define RB_CAP 8u
typedef struct { uint32_t buf[RB_CAP]; uint32_t head, tail, count; } ringbuf_t;
void rb_init(ringbuf_t *rb);
int rb_push(ringbuf_t *rb, uint32_t v);   /* 0 ok, -1 full  */
int rb_pop(ringbuf_t *rb, uint32_t *out); /* 0 ok, -1 empty */
#endif
