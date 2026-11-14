// macOS: an application class that Content's event handling expects, and the sandbox's entry for child processes,
// which run this same executable (platform.h).

#import <AppKit/AppKit.h>

#include <stdio.h>

#include <optional>

#include "base/command_line.h"
#include "base/mac/scoped_sending_event.h"
#include "base/message_loop/message_pump_apple.h"
#include "base/observer_list.h"
#include "content/public/browser/native_event_processor_mac.h"
#include "content/public/browser/native_event_processor_observer_mac.h"
#include "content/public/browser/web_contents.h"
#include "sandbox/mac/seatbelt_exec.h"
#include "tests/embedder/platform.h"

@interface EmbedderApplication : NSApplication <CrAppProtocol, CrAppControlProtocol, NativeEventProcessor>
@end

@implementation EmbedderApplication {
  BOOL _handlingSendEvent;
  base::ObserverList<content::NativeEventProcessorObserver>::Unchecked _observers;
}

- (void)addNativeEventProcessorObserver:(content::NativeEventProcessorObserver*)observer {
  _observers.AddObserver(observer);
}

- (void)removeNativeEventProcessorObserver:(content::NativeEventProcessorObserver*)observer {
  _observers.RemoveObserver(observer);
}

- (BOOL)isHandlingSendEvent {
  return _handlingSendEvent;
}

- (void)setHandlingSendEvent:(BOOL)handlingSendEvent {
  _handlingSendEvent = handlingSendEvent;
}

- (void)sendEvent:(NSEvent*)event {
  base::mac::ScopedSendingEvent sending;
  content::ScopedNotifyNativeEventProcessorObserver notify(&_observers, event);
  [super sendEvent:event];
}

@end

namespace embedder {

int EarlyMain(int argc, const char** argv) {
  // A sandboxed child enters the sandbox its parent prepared before anything else runs.
  sandbox::SeatbeltExecServer::CreateFromArgumentsResult seatbelt =
      sandbox::SeatbeltExecServer::CreateFromArguments(argv[0], argc, argv);
  if (seatbelt.sandbox_required && !(seatbelt.server && seatbelt.server->InitializeSandbox())) {
    fprintf(stderr, "embedder: cannot enter the sandbox\n");
    return 1;
  }
  return -1;
}

void AppendPlatformSwitches(base::CommandLine&) {}

std::optional<int> PreBrowserMain() {
  [EmbedderApplication sharedApplication];
  [NSApp setActivationPolicy:NSApplicationActivationPolicyProhibited];
  return std::nullopt;
}

void PrepareWindows() {}

void ShowContents(content::WebContents*) {}

void HideContents() {}

}  // namespace embedder
