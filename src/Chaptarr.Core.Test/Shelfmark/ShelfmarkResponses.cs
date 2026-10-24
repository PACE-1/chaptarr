namespace Chaptarr.Core.Test.Shelfmark
{
    // Shaped exactly as Shelfmark (calibrain/shelfmark) serializes them: /api/releases returns
    // dataclasses.asdict(Release) per release, and /api/status groups _task_to_dict output by queue state.
    public static class ShelfmarkResponses
    {
        public const string Releases = @"{
  ""releases"": [
    {
      ""source"": ""direct_download"",
      ""source_id"": ""5c1b6f0e2f6a4b8e9d0c1a2b3c4d5e6f"",
      ""title"": ""Project Hail Mary"",
      ""format"": ""epub"",
      ""language"": ""en"",
      ""size"": ""2.4MB"",
      ""size_bytes"": null,
      ""download_url"": null,
      ""info_url"": ""https://annas-archive.example/md5/5c1b6f0e2f6a4b8e9d0c1a2b3c4d5e6f"",
      ""protocol"": null,
      ""indexer"": null,
      ""seeders"": null,
      ""peers"": null,
      ""content_type"": null,
      ""extra"": { ""author"": ""Andy Weir"", ""year"": ""2021"", ""publisher"": ""Ballantine"" }
    },
    {
      ""source"": ""prowlarr"",
      ""source_id"": ""prowlarr-1234"",
      ""title"": ""Andy Weir - Project Hail Mary (2021) [Ray Porter] M4B"",
      ""format"": ""m4b"",
      ""language"": null,
      ""size"": ""1.1 GB"",
      ""size_bytes"": 1181116006,
      ""download_url"": ""magnet:?xt=urn:btih:abc"",
      ""info_url"": null,
      ""protocol"": ""torrent"",
      ""indexer"": ""MyAnonamouse"",
      ""seeders"": 42,
      ""peers"": ""42/0"",
      ""content_type"": ""audiobook"",
      ""extra"": { ""narrator"": ""Ray Porter"" }
    },
    {
      ""source"": ""direct_download"",
      ""source_id"": """",
      ""title"": ""Missing id is skipped"",
      ""extra"": {}
    }
  ],
  ""book"": { ""provider"": ""manual"", ""provider_id"": ""chaptarr-12"", ""title"": ""Project Hail Mary"" },
  ""sources_searched"": [""direct_download"", ""prowlarr""],
  ""column_config"": null,
  ""search_info"": {}
}";

        public const string Status = @"{
  ""queued"": {
    ""q1"": { ""id"": ""q1"", ""title"": ""Queued Book"", ""author"": ""Jane Doe"", ""size"": ""1 MB"", ""content_type"": ""ebook"", ""progress"": 0, ""status"": ""queued"", ""status_message"": null, ""download_path"": null, ""user_id"": 3, ""username"": ""isaac"" }
  },
  ""downloading"": {
    ""d1"": { ""id"": ""d1"", ""title"": ""Downloading Book"", ""author"": ""Jane Doe"", ""size"": ""100 MB"", ""content_type"": ""audiobook"", ""progress"": 25.0, ""status"": ""downloading"", ""status_message"": ""Downloading"", ""download_path"": null, ""user_id"": 3, ""username"": ""isaac"" }
  },
  ""complete"": {
    ""c1"": { ""id"": ""c1"", ""title"": ""Jane Doe - Finished Book"", ""author"": ""Jane Doe"", ""size"": ""2 MB"", ""content_type"": ""ebook"", ""progress"": 100, ""status"": ""complete"", ""status_message"": ""Complete"", ""download_path"": ""/books/isaac/Finished Book.epub"", ""user_id"": 3, ""username"": ""isaac"" },
    ""c2"": { ""id"": ""c2"", ""title"": ""Emailed Book"", ""author"": null, ""size"": null, ""content_type"": ""ebook"", ""progress"": 100, ""status"": ""complete"", ""status_message"": ""Sent"", ""download_path"": null, ""user_id"": 3, ""username"": ""isaac"" }
  },
  ""error"": {
    ""e1"": { ""id"": ""e1"", ""title"": ""Broken Book"", ""author"": ""Jane Doe"", ""size"": null, ""content_type"": ""ebook"", ""progress"": 0, ""status"": ""error"", ""status_message"": ""All mirrors failed"", ""download_path"": null, ""user_id"": 3, ""username"": ""isaac"" }
  },
  ""cancelled"": {
    ""x1"": { ""id"": ""x1"", ""title"": ""Someone Else's Book"", ""author"": ""Jane Doe"", ""size"": null, ""content_type"": ""ebook"", ""progress"": 0, ""status"": ""cancelled"", ""status_message"": null, ""download_path"": null, ""user_id"": 9, ""username"": ""paiten"" }
  }
}";
    }
}
