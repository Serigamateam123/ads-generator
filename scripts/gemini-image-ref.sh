#!/usr/bin/env bash
# gemini-image-ref.sh — Generate an image via Gemini 3 Pro Image Preview
#
# Usage:
#   gemini-image-ref.sh "<prompt>" "<aspect_ratio>" "<output_path>" [<reference_image_path>]
#
# Args:
#   prompt               — Full text prompt for image generation
#   aspect_ratio         — e.g. "9:16", "1:1", "16:9"
#   output_path          — Where to save the generated PNG
#   reference_image_path — (optional) Path to a reference image attached as inline_data
#
# Requires: curl, perl, GEMINI_API_KEY env var

set -euo pipefail

PROMPT="${1:?Usage: gemini-image-ref.sh <prompt> <aspect_ratio> <output_path> [ref_image_path]}"
ASPECT_RATIO="${2:?aspect_ratio required}"
OUTPUT_PATH="${3:?output_path required}"
REF_IMAGE_PATH="${4:-}"

API_KEY="${GEMINI_API_KEY:?GEMINI_API_KEY environment variable is not set}"
API_URL="https://generativelanguage.googleapis.com/v1beta/models/gemini-3-pro-image-preview:generateContent?key=${API_KEY}"

# Build the JSON payload using Perl (portable: works on Git Bash for Windows)
# Perl handles base64 encoding and JSON string escaping safely.

PAYLOAD=$(perl - "$PROMPT" "$ASPECT_RATIO" "$REF_IMAGE_PATH" <<'PERL'
use strict;
use warnings;
use MIME::Base64 qw(encode_base64);
use JSON; # fallback manual if JSON unavailable

my ($prompt, $aspect, $ref_path) = @ARGV;

# JSON-escape a string
sub je {
    my ($s) = @_;
    $s =~ s/\\/\\\\/g;
    $s =~ s/"/\\"/g;
    $s =~ s/\n/\\n/g;
    $s =~ s/\r/\\r/g;
    $s =~ s/\t/\\t/g;
    return $s;
}

my $prompt_escaped = je($prompt);

my $ref_part = "";
if ($ref_path && -f $ref_path) {
    # Detect mime type from extension
    my $mime = "image/jpeg";
    if ($ref_path =~ /\.png$/i)  { $mime = "image/png"; }
    if ($ref_path =~ /\.webp$/i) { $mime = "image/webp"; }
    if ($ref_path =~ /\.gif$/i)  { $mime = "image/gif"; }

    open(my $fh, '<:raw', $ref_path) or die "Cannot open $ref_path: $!";
    local $/;
    my $raw = <$fh>;
    close($fh);

    my $b64 = encode_base64($raw, "");  # no newlines

    $ref_part = qq|,{"inline_data":{"mime_type":"$mime","data":"$b64"}}|;
}

print qq|{
  "contents": [
    {
      "parts": [
        {"text": "$prompt_escaped"}$ref_part
      ]
    }
  ],
  "generationConfig": {
    "responseModalities": ["IMAGE"],
    "imageConfig": {
      "aspectRatio": "$aspect"
    }
  }
}|;
PERL
)

# POST to Gemini API, capture full response
TMPFILE=$(mktemp /tmp/gemini_resp_XXXXXX.json)
HTTP_CODE=$(curl -s -w "%{http_code}" -o "$TMPFILE" \
    -X POST "$API_URL" \
    -H "Content-Type: application/json" \
    -d "$PAYLOAD")

if [ "$HTTP_CODE" != "200" ]; then
    echo "ERROR: Gemini API returned HTTP $HTTP_CODE" >&2
    cat "$TMPFILE" >&2
    rm -f "$TMPFILE"
    exit 1
fi

# Extract the base64 image data from the response using Perl
perl - "$TMPFILE" "$OUTPUT_PATH" <<'PERL'
use strict;
use warnings;
use MIME::Base64 qw(decode_base64);

my ($resp_file, $out_path) = @ARGV;

open(my $fh, '<', $resp_file) or die "Cannot open response file: $!";
local $/;
my $json = <$fh>;
close($fh);

# Extract base64 data — look for "data": "<base64>" inside inlineData or inline_data
my ($b64) = $json =~ /"data"\s*:\s*"([A-Za-z0-9+\/=\n]+)"/;
unless ($b64) {
    die "Could not find image data in Gemini response. Full response:\n$json\n";
}
$b64 =~ s/\s+//g;  # strip any embedded whitespace/newlines

my $img_bytes = decode_base64($b64);

# Ensure output directory exists
my $dir = $out_path;
$dir =~ s|/[^/]+$||;
unless (-d $dir) {
    system("mkdir", "-p", $dir) == 0 or die "Cannot create output dir $dir";
}

open(my $out, '>:raw', $out_path) or die "Cannot write to $out_path: $!";
print $out $img_bytes;
close($out);

print "Saved: $out_path\n";
PERL

rm -f "$TMPFILE"
echo "Done: $OUTPUT_PATH"
