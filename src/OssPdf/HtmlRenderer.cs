using System.Globalization;
using Microsoft.Extensions.Options;
using Microsoft.Playwright;

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

public sealed class HtmlRenderer(IOptions<PdfLimits> options) : IAsyncDisposable
{
    private readonly SemaphoreSlim _startup = new(1);
    private IPlaywright? _playwright;
    private IBrowser? _browser;

    public async Task RenderAsync(HtmlToPdfRequest request, PdfJob job, CancellationToken cancellationToken)
    {
        if (string.IsNullOrWhiteSpace(request.Body))
        {
            throw new PdfRequestException(400, "An HTML body is required.");
        }

        var print = new PagePdfOptions
        {
            Format = "Letter",
            Path = job.OutputPath,
            DisplayHeaderFooter = request.Header is not null || request.Footer is not null,
            HeaderTemplate = request.Header ?? "<span></span>",
            FooterTemplate = request.Footer ?? "<span></span>",
            PrintBackground = true,
            Margin = new Margin
            {
                Top = Millimeters(request.MarginTop, request.Header is null ? 12 : 20),
                Bottom = Millimeters(request.MarginBottom, request.Footer is null ? 12 : 20),
                Left = Millimeters(request.MarginLeft, 12),
                Right = Millimeters(request.MarginRight, 12)
            }
        };

        var browser = await GetBrowserAsync(cancellationToken);
        var context = await browser.NewContextAsync(new BrowserNewContextOptions
        {
            JavaScriptEnabled = false,
            ServiceWorkers = ServiceWorkerPolicy.Block,
            Offline = true,
            AcceptDownloads = false
        });
        try
        {
            await context.RouteAsync("**/*", route => route.AbortAsync());
            var page = await context.NewPageAsync();
            await page.SetContentAsync(request.Body, new PageSetContentOptions
            {
                WaitUntil = WaitUntilState.Load,
                Timeout = options.Value.TimeoutSeconds * 1000f
            }).WaitAsync(cancellationToken);
            await page.PdfAsync(print).WaitAsync(cancellationToken);
        }
        catch (PlaywrightException) when (cancellationToken.IsCancellationRequested)
        {
            cancellationToken.ThrowIfCancellationRequested();
            throw;
        }
        catch (PlaywrightException) when (browser.IsConnected)
        {
            throw new PdfRequestException(422, "Chromium could not render the HTML document.");
        }
        finally
        {
            await context.CloseAsync();
        }
    }

    private async Task<IBrowser> GetBrowserAsync(CancellationToken cancellationToken)
    {
        await _startup.WaitAsync(cancellationToken);
        try
        {
            if (_browser is { IsConnected: true })
            {
                return _browser;
            }

            _playwright ??= await Playwright.CreateAsync();
            _browser = await _playwright.Chromium.LaunchAsync(new BrowserTypeLaunchOptions
            {
                Headless = true,
                ChromiumSandbox = options.Value.ChromiumSandbox,
                Timeout = options.Value.TimeoutSeconds * 1000f
            });
            return _browser;
        }
        finally
        {
            _startup.Release();
        }
    }

    private static string Millimeters(double? value, double fallback)
    {
        var millimeters = value ?? fallback;
        if (!double.IsFinite(millimeters) || millimeters is < 0 or > 100)
        {
            throw new PdfRequestException(400, "Margins must be between 0 and 100 millimeters.");
        }

        return millimeters.ToString("0.###", CultureInfo.InvariantCulture) + "mm";
    }

    public async ValueTask DisposeAsync()
    {
        if (_browser is not null)
        {
            await _browser.CloseAsync();
        }

        _playwright?.Dispose();
        _startup.Dispose();
    }
}
