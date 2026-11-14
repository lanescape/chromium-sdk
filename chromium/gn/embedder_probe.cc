// An embedder in miniature (BUILD.gn): it includes headers an embedder starts from and refers to the library's
// exports, so building it shows that the SDK's flags compile and link. It is never run.

#include "base/command_line.h"
#include "base/run_loop.h"
#include "content/public/app/content_main.h"
#include "content/public/app/content_main_delegate.h"
#include "content/public/browser/browser_context.h"
#include "content/public/browser/browser_main_parts.h"
#include "content/public/browser/content_browser_client.h"
#include "content/public/browser/render_frame_host.h"
#include "content/public/browser/web_contents.h"
#include "content/public/browser/web_contents_observer.h"
#include "content/public/common/content_client.h"
#include "content/public/renderer/content_renderer_client.h"
#include "third_party/blink/public/web/web_local_frame.h"
#include "ui/gfx/geometry/size.h"
#include "url/gurl.h"

int main(int argc, const char** argv) {
  content::ContentMainDelegate delegate;
  content::ContentMainParams params(&delegate);
  params.argc = argc;
  params.argv = argv;
  return content::ContentMain(std::move(params));
}
