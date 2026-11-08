#include <stdio.h>
#include "../src/ringbuf.h"
#define CHECK(c) do { if (!(c)) { printf("FAIL %s:%d %s\n", __FILE__, __LINE__, #c); return 1; } } while (0)
int main(void) {
    ringbuf_t rb; uint32_t v; rb_init(&rb);
    CHECK(rb_pop(&rb, &v) == -1);
    for (uint32_t i = 0; i < RB_CAP; i++) CHECK(rb_push(&rb, i) == 0);
    CHECK(rb_push(&rb, 99) == -1);
    for (uint32_t i = 0; i < RB_CAP; i++) { CHECK(rb_pop(&rb, &v) == 0); CHECK(v == i); }
    CHECK(rb_push(&rb, 7) == 0 && rb_pop(&rb, &v) == 0 && v == 7);  /* wrap-around */
    printf("ok: 5 checks\n"); return 0;
}
