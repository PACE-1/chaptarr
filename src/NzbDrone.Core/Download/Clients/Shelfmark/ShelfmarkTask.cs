using Newtonsoft.Json;

namespace NzbDrone.Core.Download.Clients.Shelfmark
{
    // One entry of Shelfmark's GET /api/status, as built by its orchestrator's _task_to_dict.
    public class ShelfmarkTask
    {
        [JsonProperty("id")]
        public string Id { get; set; }

        [JsonProperty("title")]
        public string Title { get; set; }

        [JsonProperty("author")]
        public string Author { get; set; }

        [JsonProperty("format")]
        public string Format { get; set; }

        [JsonProperty("size")]
        public string Size { get; set; }

        [JsonProperty("content_type")]
        public string ContentType { get; set; }

        [JsonProperty("source")]
        public string Source { get; set; }

        [JsonProperty("progress")]
        public double? Progress { get; set; }

        [JsonProperty("status")]
        public string Status { get; set; }

        [JsonProperty("status_message")]
        public string StatusMessage { get; set; }

        [JsonProperty("download_path")]
        public string DownloadPath { get; set; }

        [JsonProperty("user_id")]
        public int? UserId { get; set; }

        [JsonProperty("username")]
        public string Username { get; set; }
    }
}
