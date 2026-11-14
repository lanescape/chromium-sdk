// Objective-C++, so the SDK's flags for it come from a real compile (BUILD.gn).

#import <Foundation/Foundation.h>

#include "base/apple/foundation_util.h"

void ChromiumSdkProbeObjectiveC() {
  @autoreleasepool {
    base::apple::FrameworkBundle();
  }
}
