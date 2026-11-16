// XInput2 probe for an XWayland click: logs, one line per event with a monotonic time in microseconds, the raw
// pointer events Xwayland makes (selected on the root, as Wine selects them) and the core events of its own window, so
// a run shows whether a RawMotion reaches X clients before the press of a click deskpilot sends. Build with
// gcc scripts/xi2_raw_probe.c -lXi -lX11, start it inside a nested Hyprland (scripts/nested.py) with that instance's
// exec dispatcher so it gets its Xwayland DISPLAY, then shot and click its "deskpilot xi2 probe" window there. It
// prints "ready WINDOW" once its window is mapped, and runs until killed.
#include <X11/Xlib.h>
#include <X11/Xutil.h>
#include <X11/extensions/XInput2.h>
#include <stdio.h>
#include <string.h>
#include <time.h>

static long long micros(void) {
  struct timespec t;
  clock_gettime(CLOCK_MONOTONIC, &t);
  return (long long)t.tv_sec * 1000000 + t.tv_nsec / 1000;
}

static void raw(const char *name, XIRawEvent *e) {
  printf("%lld %s time=%lu device=%d source=%d detail=%d", micros(), name, e->time, e->deviceid, e->sourceid, e->detail);
  const double *value = e->valuators.values, *rawValue = e->raw_values;
  for (int i = 0; i < e->valuators.mask_len * 8; i++)
    if (XIMaskIsSet(e->valuators.mask, i))
      printf(" v%d=%g/%g", i, *value++, *rawValue++);
  printf("\n");
}

int main(void) {
  Display *display = XOpenDisplay(NULL);
  if (!display)
    return 2;
  int opcode, event, error, major = 2, minor = 2;
  if (!XQueryExtension(display, "XInputExtension", &opcode, &event, &error) ||
      XIQueryVersion(display, &major, &minor) != Success)
    return 3;
  Window root = DefaultRootWindow(display);
  Window window = XCreateSimpleWindow(display, root, 0, 0, 600, 400, 0, 0, 0xffffff);
  XStoreName(display, window, "deskpilot xi2 probe");
  XSelectInput(display, window,
               ButtonPressMask | ButtonReleaseMask | PointerMotionMask | EnterWindowMask | LeaveWindowMask |
                   StructureNotifyMask);
  unsigned char bits[XIMaskLen(XI_LASTEVENT)] = {0};
  XISetMask(bits, XI_RawMotion);
  XISetMask(bits, XI_RawButtonPress);
  XISetMask(bits, XI_RawButtonRelease);
  XIEventMask mask = {XIAllMasterDevices, sizeof(bits), bits};
  XISelectEvents(display, root, &mask, 1);
  XMapWindow(display, window);
  setvbuf(stdout, NULL, _IOLBF, 0);
  for (;;) {
    XEvent e;
    XNextEvent(display, &e);
    XGenericEventCookie *cookie = &e.xcookie;
    if (cookie->type == GenericEvent && cookie->extension == opcode && XGetEventData(display, cookie)) {
      const char *name = cookie->evtype == XI_RawMotion        ? "RawMotion"
                         : cookie->evtype == XI_RawButtonPress ? "RawButtonPress"
                                                                : "RawButtonRelease";
      raw(name, cookie->data);
      XFreeEventData(display, cookie);
      continue;
    }
    Window r, c;
    int rx, ry, wx, wy;
    unsigned int m;
    switch (e.type) {
    case MapNotify:
      printf("ready 0x%lx\n", window);
      break;
    case ButtonPress:
    case ButtonRelease:
      XQueryPointer(display, window, &r, &c, &rx, &ry, &wx, &wy, &m);
      printf("%lld %s time=%lu button=%u at=%d,%d root=%d,%d pointer=%d,%d\n", micros(),
             e.type == ButtonPress ? "ButtonPress" : "ButtonRelease", e.xbutton.time, e.xbutton.button, e.xbutton.x,
             e.xbutton.y, e.xbutton.x_root, e.xbutton.y_root, wx, wy);
      break;
    case MotionNotify:
      printf("%lld Motion time=%lu at=%d,%d\n", micros(), e.xmotion.time, e.xmotion.x, e.xmotion.y);
      break;
    case EnterNotify:
    case LeaveNotify:
      printf("%lld %s time=%lu at=%d,%d\n", micros(), e.type == EnterNotify ? "Enter" : "Leave", e.xcrossing.time,
             e.xcrossing.x, e.xcrossing.y);
      break;
    }
  }
}
