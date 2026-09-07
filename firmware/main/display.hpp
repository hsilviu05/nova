// The panel, as far as the face is concerned.
//
// Everything about *what* the face looks like is in core/face.hpp and is
// tested there. This is the part that cannot be: a framebuffer in PSRAM, a
// span blitter, and a flush.
//
// **The controller is injected, not chosen here.** `begin` takes a panel
// handle that the caller has already brought up with the right vendor
// component for the board in hand. That is deliberate: an AMOLED's
// initialisation is a vendor-specific command list, and inventing one from
// memory is how a display stays black with no error to explain it. Naming the
// component is a job for whoever has the board on the desk; everything below
// this line works the same whichever one it is.

#pragma once

#include <cstdint>

#include "esp_err.h"
#include "esp_lcd_panel_ops.h"
#include "nova/face.hpp"

namespace nova::hw {

/// Bytes per pixel in the framebuffer. RGB565 is what esp_lcd speaks and what
/// the panel wants; 24-bit colour would cost 600 KB for a face that uses two.
inline constexpr size_t kBytesPerPixel = 2;

/// Convert to the panel's 16-bit format.
///
/// The panel takes RGB565 big-endian over QSPI. Getting the byte order wrong
/// swaps red and blue, which looks like a colour choice rather than a bug and
/// so tends to survive a long time.
constexpr uint16_t to_rgb565(const Rgb &colour) {
    const uint16_t value = static_cast<uint16_t>(((colour.r & 0xF8) << 8) |
                                                 ((colour.g & 0xFC) << 3) |
                                                 (colour.b >> 3));
    return static_cast<uint16_t>((value >> 8) | (value << 8));
}

class Display {
   public:
    /// Take ownership of an already-initialised panel and allocate the
    /// framebuffer. Returns ESP_ERR_NO_MEM if PSRAM cannot supply it -- 410 x
    /// 502 x 2 is about 412 KB, which will not fit in internal RAM.
    esp_err_t begin(esp_lcd_panel_handle_t panel);

    /// Draw a face and push only what changed.
    ///
    /// The bounding box of the old face and the new one is the region sent
    /// over the wire. A full-screen flush is 412 KB per frame; a blinking
    /// pair of eyes is nearer 30 KB, and the panel is the largest power draw
    /// on the board.
    esp_err_t draw(const Face &face);

    /// Blank the panel. Used when the device sleeps deeply: an unlit AMOLED
    /// pixel draws nothing at all, so this is the lowest-power state there is.
    esp_err_t clear();

    bool available() const { return panel_ != nullptr && frame_ != nullptr; }

   private:
    struct Box {
        int16_t x0 = 0, y0 = 0, x1 = -1, y1 = -1;
        bool empty() const { return x1 < x0 || y1 < y0; }
        void add(const Eye &eye);
        void merge(const Box &other);
    };

    void paint(const Eye &eye, uint16_t colour);
    esp_err_t flush(const Box &box);

    esp_lcd_panel_handle_t panel_ = nullptr;
    uint16_t *frame_ = nullptr;
    Box last_{};
    bool drawn_ = false;
};

}  // namespace nova::hw
