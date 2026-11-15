// Objective-C++, so the SDK's flags for it come from a real compile, and its link names what it uses (BUILD.gn).

#import <Foundation/Foundation.h>

#include <string>

#include "base/strings/sys_string_conversions.h"

std::string LibchromiumProbeObjectiveC() {
  @autoreleasepool {
    return base::SysNSStringToUTF8(@"libchromium");
  }
}
