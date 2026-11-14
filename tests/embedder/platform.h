// What differs by system in the test embedder (embedder.cc).

#ifndef TESTS_EMBEDDER_PLATFORM_H_
#define TESTS_EMBEDDER_PLATFORM_H_

#include <optional>

namespace base {
class CommandLine;
}
namespace content {
class WebContents;
}

namespace embedder {

// Runs before anything else; returns an exit code to stop with, or a negative number to carry on.
int EarlyMain(int argc, const char** argv);
// Switches the browser process adds to its own command line, which its children inherit where they need to.
void AppendPlatformSwitches(base::CommandLine& command_line);
std::optional<int> PreBrowserMain();
// Puts the page in a window of its own, so it renders as it would on screen, and takes the window away.
void ShowContents(content::WebContents* contents);
void HideContents();

}  // namespace embedder

#endif  // TESTS_EMBEDDER_PLATFORM_H_
