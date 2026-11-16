using Microsoft.Extensions.Options;

namespace OssPdf;

public sealed record HtmlToPdfRequest(
    string Body,
    string? Header,
    string? Footer,
    string? FileName,
    double? MarginTop,
    double? MarginBottom,
    double? MarginRight,
    double? MarginLeft);

internal sealed record HtmlPageMargins(double Top, double Right, double Bottom, double Left);

public sealed class HtmlRenderer(IOptions<PdfLimits> options, IronpressWorker ironpress)
{
    public Task RenderAsync(HtmlToPdfRequest request, PdfJob job, CancellationToken cancellationToken)
    {
        if (string.IsNullOrWhiteSpace(request.Body))
        {
            throw new PdfRequestException(400, "An HTML body is required.");
        }

        var margins = new HtmlPageMargins(
            Millimeters(request.MarginTop, request.Header is null ? 12 : 20),
            Millimeters(request.MarginRight, 12),
            Millimeters(request.MarginBottom, request.Footer is null ? 12 : 20),
            Millimeters(request.MarginLeft, 12));

        return ironpress.RenderAsync(request, margins, job, options.Value.MaxPages, cancellationToken);
    }

    private static double Millimeters(double? value, double fallback)
    {
        var millimeters = value ?? fallback;
        if (!double.IsFinite(millimeters) || millimeters is < 0 or > 100)
        {
            throw new PdfRequestException(400, "Margins must be between 0 and 100 millimeters.");
        }

        return millimeters;
    }
}
