using OssPdf;
using Wolverine;
using Wolverine.Http;

if (args is ["--install-chromium"])
{
    return Microsoft.Playwright.Program.Main(["install", "--with-deps", "--only-shell", "chromium"]);
}

var builder = WebApplication.CreateBuilder(args);
builder.WebHost.ConfigureKestrel(options => options.Limits.MaxRequestBodySize = PdfLimits.RequestBytes);
builder.Services.AddProblemDetails();
builder.Services.AddOptions<PdfLimits>()
    .BindConfiguration("Pdf")
    .Validate(limits => limits.IsValid(), "Invalid PDF processing limits.")
    .ValidateOnStart();
builder.Services.AddSingleton<PdfJobs>();
builder.Services.AddSingleton<PdfConverters>();
builder.Services.AddSingleton<HtmlRenderer>();
builder.Host.UseWolverine(options => options.Durability.Mode = DurabilityMode.Solo);
builder.Services.AddWolverineHttp();

var app = builder.Build();
app.UseExceptionHandler();
app.Use(async (context, next) =>
{
    try
    {
        if (context.Request.ContentLength > PdfLimits.RequestBytes)
        {
            throw new PdfRequestException(413, "The request exceeds 60 MB.");
        }

        await next(context);
    }
    catch (BadHttpRequestException exception) when (!context.Response.HasStarted)
    {
        var title = exception.StatusCode == 413 ? "The request exceeds 60 MB." : "Invalid request.";
        await Results.Problem(statusCode: exception.StatusCode, title: title).ExecuteAsync(context);
    }
    catch (PdfRequestException exception) when (!context.Response.HasStarted)
    {
        await Results.Problem(statusCode: exception.StatusCode, title: exception.Message).ExecuteAsync(context);
    }
});
app.MapWolverineEndpoints();
await app.RunAsync();
return 0;
