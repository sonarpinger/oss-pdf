#include <errno.h>
#include <inttypes.h>
#include <math.h>
#include <png.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>

#include "fpdf_formfill.h"
#include "fpdfview.h"

static bool positive_integer(const char *text, uint64_t *value)
{
    char *end;
    errno = 0;
    *value = strtoumax(text, &end, 10);
    return text[0] >= '1' && text[0] <= '9' && *end == '\0' && errno == 0;
}

static bool write_page(FPDF_PAGE page, FPDF_FORMHANDLE forms, int index,
                       int width, int height)
{
    FPDF_BITMAP bitmap = FPDFBitmap_CreateEx(width, height, FPDFBitmap_BGR, NULL, 0);
    if (bitmap == NULL) {
        return false;
    }

    FPDFBitmap_FillRect(bitmap, 0, 0, width, height, 0xffffffff);
    if (forms != NULL) {
        FORM_OnAfterLoadPage(page, forms);
    }
    FPDF_RenderPageBitmap(bitmap, page, 0, 0, width, height, 0, FPDF_ANNOT);
    if (forms != NULL) {
        FPDF_FFLDraw(forms, bitmap, page, 0, 0, width, height, 0, FPDF_ANNOT);
        FORM_OnBeforeClosePage(page, forms);
    }

    char path[32];
    snprintf(path, sizeof(path), "page-%06d.png", index);
    png_image image = {
        .version = PNG_IMAGE_VERSION,
        .width = (png_uint_32)width,
        .height = (png_uint_32)height,
        .format = PNG_FORMAT_BGR
    };
    bool written = png_image_write_to_file(&image, path, 0, FPDFBitmap_GetBuffer(bitmap),
                                           FPDFBitmap_GetStride(bitmap), NULL);
    png_image_free(&image);
    FPDFBitmap_Destroy(bitmap);
    return written;
}

static bool render_document(FPDF_DOCUMENT document, uint64_t dpi, uint64_t max_pages,
                            uint64_t max_page_pixels, uint64_t remaining_pixels)
{
    const int page_count = FPDF_GetPageCount(document);
    if (page_count <= 0 || (uint64_t)page_count > max_pages ||
        FPDF_GetSecurityHandlerRevision(document) != -1) {
        fprintf(stderr, "The PDF is encrypted or has an invalid page count.\n");
        return false;
    }

    // Render form appearances without running any document actions or scripts.
    FPDF_FORMFILLINFO form_info = { .version = 1 };
    FPDF_FORMHANDLE forms = FPDFDOC_InitFormFillEnvironment(document, &form_info);
    bool success = true;
    for (int index = 0; index < page_count; index++) {
        FPDF_PAGE page = FPDF_LoadPage(document, index);
        if (page == NULL) {
            success = false;
            break;
        }

        const double width = FPDF_GetPageWidthF(page);
        const double height = FPDF_GetPageHeightF(page);
        const double columns = ceil(width * (double)dpi / 72);
        const double rows = ceil(height * (double)dpi / 72);
        // ImageMagick's policy also limits the dimensions of each bitmap.
        if (!(columns >= 1 && columns <= 16384 && rows >= 1 && rows <= 16384)) {
            FPDF_ClosePage(page);
            success = false;
            break;
        }

        const uint64_t pixels = (uint64_t)columns * (uint64_t)rows;
        success = pixels <= max_page_pixels && pixels <= remaining_pixels &&
                  write_page(page, forms, index + 1, (int)columns, (int)rows);
        FPDF_ClosePage(page);
        if (!success) {
            break;
        }

        remaining_pixels -= pixels;
        printf("%.9g %.9g\n", width, height);
    }

    if (forms != NULL) {
        FPDFDOC_ExitFormFillEnvironment(forms);
    }
    if (!success) {
        fprintf(stderr, "Unable to render a PDF page within the configured limits.\n");
    }
    return success;
}

int main(int argc, char *argv[])
{
    uint64_t dpi, max_pages, max_page_pixels, max_total_pixels;
    if (argc != 6 || !positive_integer(argv[2], &dpi) ||
        !positive_integer(argv[3], &max_pages) || !positive_integer(argv[4], &max_page_pixels) ||
        !positive_integer(argv[5], &max_total_pixels)) {
        fprintf(stderr, "Usage: pdfium-render INPUT DPI MAX_PAGES MAX_PAGE_PIXELS MAX_TOTAL_PIXELS\n");
        return EXIT_FAILURE;
    }

    FPDF_InitLibrary();
    FPDF_DOCUMENT document = FPDF_LoadDocument(argv[1], NULL);
    if (document == NULL) {
        fprintf(stderr, "Unable to open the PDF.\n");
        FPDF_DestroyLibrary();
        return EXIT_FAILURE;
    }

    bool success = render_document(document, dpi, max_pages, max_page_pixels, max_total_pixels);
    FPDF_CloseDocument(document);
    FPDF_DestroyLibrary();
    return success ? EXIT_SUCCESS : EXIT_FAILURE;
}
