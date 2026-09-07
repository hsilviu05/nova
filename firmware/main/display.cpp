#include "display.hpp"

#include <cstring>

#include "esp_heap_caps.h"
#include "esp_log.h"

namespace nova::hw {

namespace {
constexpr const char *kTag = "face";
constexpr size_t kPixels = static_cast<size_t>(kPanelWidth) * kPanelHeight;
}  // namespace

void Display::Box::add(const Eye &eye) {
    const Box other{eye.left(), eye.top(), static_cast<int16_t>(eye.right() - 1),
                    static_cast<int16_t>(eye.bottom() - 1)};
    merge(other);
}

void Display::Box::merge(const Box &other) {
    if (other.empty()) {
        return;
    }
    if (empty()) {
        *this = other;
        return;
    }
    x0 = other.x0 < x0 ? other.x0 : x0;
    y0 = other.y0 < y0 ? other.y0 : y0;
    x1 = other.x1 > x1 ? other.x1 : x1;
    y1 = other.y1 > y1 ? other.y1 : y1;
}

esp_err_t Display::begin(esp_lcd_panel_handle_t panel) {
    if (panel == nullptr) {
        return ESP_ERR_INVALID_ARG;
    }
    panel_ = panel;

    frame_ = static_cast<uint16_t *>(
        heap_caps_malloc(kPixels * kBytesPerPixel, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
    if (frame_ == nullptr) {
        ESP_LOGE(kTag, "no PSRAM for a %ux%u framebuffer", kPanelWidth, kPanelHeight);
        panel_ = nullptr;
        return ESP_ERR_NO_MEM;
    }

    std::memset(frame_, 0, kPixels * kBytesPerPixel);
    return clear();
}

void Display::paint(const Eye &eye, uint16_t colour) {
    rasterise(eye, [&](Span span) {
        // The spans are proved on-panel by the static assertions in
        // core/face.cpp, so this writes them without bounds-checking.
        uint16_t *row = frame_ + static_cast<size_t>(span.y) * kPanelWidth;
        for (int16_t x = span.x0; x <= span.x1; ++x) {
            row[x] = colour;
        }
    });
}

esp_err_t Display::flush(const Box &box) {
    if (box.empty()) {
        return ESP_OK;
    }
    // esp_lcd wants a contiguous buffer for the region, and the framebuffer
    // rows are panel-width apart, so the region goes row by row. Still far
    // less traffic than a full frame: two eyes are about 7% of the panel.
    for (int16_t y = box.y0; y <= box.y1; ++y) {
        const uint16_t *row = frame_ + static_cast<size_t>(y) * kPanelWidth + box.x0;
        const esp_err_t err = esp_lcd_panel_draw_bitmap(
            panel_, box.x0, y, box.x1 + 1, y + 1, row);
        if (err != ESP_OK) {
            return err;
        }
    }
    return ESP_OK;
}

esp_err_t Display::draw(const Face &face) {
    if (!available()) {
        return ESP_ERR_INVALID_STATE;
    }

    Box next;
    next.add(face.left);
    next.add(face.right);

    // Erase where the eyes were, draw where they are, and send the union.
    // Erasing only the previous bounding box rather than the whole frame is
    // what keeps a blink cheap.
    Box dirty = next;
    dirty.merge(last_);

    if (!dirty.empty()) {
        for (int16_t y = dirty.y0; y <= dirty.y1; ++y) {
            uint16_t *row = frame_ + static_cast<size_t>(y) * kPanelWidth;
            std::memset(row + dirty.x0, 0,
                        static_cast<size_t>(dirty.x1 - dirty.x0 + 1) * kBytesPerPixel);
        }
    }

    const uint16_t colour = to_rgb565(face.colour);
    paint(face.left, colour);
    paint(face.right, colour);

    const esp_err_t err = flush(dirty);
    if (err == ESP_OK) {
        last_ = next;
        drawn_ = true;
    }
    return err;
}

esp_err_t Display::clear() {
    if (frame_ == nullptr || panel_ == nullptr) {
        return ESP_ERR_INVALID_STATE;
    }
    std::memset(frame_, 0, kPixels * kBytesPerPixel);
    last_ = Box{};
    drawn_ = false;
    return flush(Box{0, 0, kPanelWidth - 1, kPanelHeight - 1});
}

}  // namespace nova::hw
