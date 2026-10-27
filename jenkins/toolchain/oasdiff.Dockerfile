# oasdiff, for demo 4's contract check: it compares a service's OpenAPI file
# with production's and lists the breaking changes. The image is pinned by
# digest; scripts/demos/apis/contract.py builds it, tagged with a hash of this
# file, and runs it without a network.
FROM tufin/oasdiff:v1.33.0@sha256:6263a96dd2ef0726c54e21fea9b8e1607eac4841add0079324b424c1f52b819c
