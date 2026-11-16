#define _GNU_SOURCE
#include "input-method.h"
#include "virtual-keyboard.h"
#include "virtual-pointer.h"
#include <stdio.h>
#include <time.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>
#include <wayland-client.h>
#include <xkbcommon/xkbcommon.h>
static struct wl_seat *seat;
static struct zwp_virtual_keyboard_manager_v1 *manager;
static struct zwlr_virtual_pointer_manager_v1 *pointers;
static struct zwp_input_method_manager_v2 *input_methods;
static void global(void *d, struct wl_registry *r, uint32_t name, const char *interface, uint32_t version) {
    if (!strcmp(interface, "wl_seat") && !seat)
        seat = wl_registry_bind(r, name, &wl_seat_interface, 1);
    if (!strcmp(interface, "zwp_virtual_keyboard_manager_v1"))
        manager = wl_registry_bind(r, name, &zwp_virtual_keyboard_manager_v1_interface, 1);
    if (!strcmp(interface, "zwlr_virtual_pointer_manager_v1"))
        pointers = wl_registry_bind(r, name, &zwlr_virtual_pointer_manager_v1_interface, 1);
    if (!strcmp(interface, "zwp_input_method_manager_v2"))
        input_methods = wl_registry_bind(r, name, &zwp_input_method_manager_v2_interface, 1);
}
static void removed(void *d, struct wl_registry *r, uint32_t name) {}
static const struct wl_registry_listener listener = {global, removed};
static struct zwp_virtual_keyboard_v1 *new_keyboard(const char *text) {
    size_t size = strlen(text) + 1;
    int fd = memfd_create("deskpilot-keymap", MFD_CLOEXEC);
    if (fd < 0 || ftruncate(fd, size) < 0 || write(fd, text, size) != (ssize_t)size)
        return NULL;
    struct zwp_virtual_keyboard_v1 *keyboard = zwp_virtual_keyboard_manager_v1_create_virtual_keyboard(manager, seat);
    zwp_virtual_keyboard_v1_keymap(keyboard, WL_KEYBOARD_KEYMAP_FORMAT_XKB_V1, fd, size);
    close(fd);
    return keyboard;
}
static uint32_t now_ms(void) {
    struct timespec now;
    clock_gettime(CLOCK_MONOTONIC, &now);
    return now.tv_sec * 1000u + now.tv_nsec / 1000000u;
}
// With "ime", this client is instead an input method that grabs the seat's keyboard and keeps every key it gets, its
// own keyboard ("key") being the one its keys reach clients through, as an input method forwards them. With "ime
// later", it grabs on the "grab" command, as an input method does once a text field is focused.
int main(int argc, char **argv) {
    int ime = argc > 1 && !strcmp(argv[1], "ime");
    struct wl_display *display = wl_display_connect(NULL);
    if (!display)
        return 1;
    struct wl_registry *registry = wl_display_get_registry(display);
    wl_registry_add_listener(registry, &listener, NULL);
    if (wl_display_roundtrip(display) < 0 || !seat || !manager)
        return 2;
    struct xkb_context *context = xkb_context_new(XKB_CONTEXT_NO_FLAGS);
    struct xkb_rule_names names = {.layout = "us"};
    struct xkb_keymap *keymap = xkb_keymap_new_from_names(context, &names, XKB_KEYMAP_COMPILE_NO_FLAGS);
    if (!keymap)
        return 3;
    char *text = xkb_keymap_get_as_string(keymap, XKB_KEYMAP_FORMAT_TEXT_V1);
    xkb_keymap_unref(keymap);
    xkb_context_unref(context);
    struct zwp_virtual_keyboard_v1 *keyboard = new_keyboard(text);
    if (!keyboard)
        return 4;
    struct zwp_input_method_v2 *input_method = NULL;
    struct zwp_input_method_keyboard_grab_v2 *grab = NULL;
    if (ime) {
        if (!input_methods)
            return 9;
        input_method = zwp_input_method_manager_v2_get_input_method(input_methods, seat);
        if (!(argc > 2 && !strcmp(argv[2], "later")))
            grab = zwp_input_method_v2_grab_keyboard(input_method);
    }
    if (wl_display_roundtrip(display) < 0)
        return 5;
    puts("ready");
    fflush(stdout);
    // The user's pointer, for moving it while a key is down: created on the first motion command.
    struct zwlr_virtual_pointer_v1 *pointer = NULL;
    // A second keyboard of the user's, sharing key state with the first in Hyprland: created on the first key2 command.
    struct zwp_virtual_keyboard_v1 *second = NULL;
    char line[256];
    while (fgets(line, sizeof(line), stdin)) {
        unsigned code, value;
        int dx, dy;
        if (sscanf(line, "key2 %u %u", &code, &value) == 2) {
            if (!second && !(second = new_keyboard(text)))
                return 8;
            zwp_virtual_keyboard_v1_key(second, now_ms(), code, value);
        } else if (sscanf(line, "key %u %u", &code, &value) == 2) {
            zwp_virtual_keyboard_v1_key(keyboard, now_ms(), code, value);
        } else if (sscanf(line, "motion %d %d", &dx, &dy) == 2) {
            if (!pointers)
                return 7;
            if (!pointer)
                pointer = zwlr_virtual_pointer_manager_v1_create_virtual_pointer(pointers, seat);
            zwlr_virtual_pointer_v1_motion(pointer, now_ms(), wl_fixed_from_int(dx), wl_fixed_from_int(dy));
            zwlr_virtual_pointer_v1_frame(pointer);
        } else if (sscanf(line, "mods %u", &value) == 1) {
            zwp_virtual_keyboard_v1_modifiers(keyboard, value, 0, 0, 0);
        } else if (!strncmp(line, "grab", 4) && input_method && !grab) {
            grab = zwp_input_method_v2_grab_keyboard(input_method);
        } else if (!strncmp(line, "quit", 4)) break;
        if (wl_display_roundtrip(display) < 0) return 6;
        puts("done"); fflush(stdout);
    }
    zwp_virtual_keyboard_v1_modifiers(keyboard, 0, 0, 0, 0);
    zwp_virtual_keyboard_v1_destroy(keyboard);
    if (second)
        zwp_virtual_keyboard_v1_destroy(second);
    free(text);
    if (grab)
        zwp_input_method_keyboard_grab_v2_release(grab);
    if (input_method)
        zwp_input_method_v2_destroy(input_method);
    if (pointer)
        zwlr_virtual_pointer_v1_destroy(pointer);
    wl_display_roundtrip(display);
    wl_display_disconnect(display);
    return 0;
}
