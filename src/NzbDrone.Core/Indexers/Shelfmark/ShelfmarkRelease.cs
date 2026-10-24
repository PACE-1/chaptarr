using System;
using System.Text;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using NzbDrone.Common.Extensions;

namespace NzbDrone.Core.Indexers.Shelfmark
{
    // A release as Shelfmark returned it. Chaptarr carries the whole object in the release's download URL,
    // so the download client can hand it back to /api/releases/download unchanged, exactly as Shelfmark's own UI does.
    public static class ShelfmarkRelease
    {
        private const string ReleaseParameter = "release=";

        public static string BuildDownloadUrl(string baseUrl, JObject release)
        {
            var json = release.ToString(Formatting.None);
            var encoded = Convert.ToBase64String(Encoding.UTF8.GetBytes(json))
                .TrimEnd('=')
                .Replace('+', '-')
                .Replace('/', '_');

            return $"{baseUrl.TrimEnd('/')}/api/releases/download?{ReleaseParameter}{encoded}";
        }

        public static bool IsShelfmarkDownloadUrl(string downloadUrl)
        {
            return downloadUrl.IsNotNullOrWhiteSpace() &&
                   downloadUrl.Contains("/api/releases/download?" + ReleaseParameter, StringComparison.Ordinal);
        }

        public static JObject ParseDownloadUrl(string downloadUrl)
        {
            if (!IsShelfmarkDownloadUrl(downloadUrl))
            {
                throw new FormatException("Not a Shelfmark release URL");
            }

            var encoded = downloadUrl.Substring(downloadUrl.IndexOf(ReleaseParameter, StringComparison.Ordinal) + ReleaseParameter.Length);
            var ampersand = encoded.IndexOf('&');

            if (ampersand >= 0)
            {
                encoded = encoded.Substring(0, ampersand);
            }

            encoded = encoded.Replace('-', '+').Replace('_', '/');
            encoded = encoded.PadRight(encoded.Length + ((4 - (encoded.Length % 4)) % 4), '=');

            var release = JObject.Parse(Encoding.UTF8.GetString(Convert.FromBase64String(encoded)));

            if (release.Value<string>("source").IsNullOrWhiteSpace() || release.Value<string>("source_id").IsNullOrWhiteSpace())
            {
                throw new FormatException("Shelfmark release is missing its source or source_id");
            }

            return release;
        }

        public static string GetTaskId(JObject release)
        {
            // Shelfmark keys its download queue by the release's source_id.
            return release.Value<string>("source_id");
        }
    }
}
