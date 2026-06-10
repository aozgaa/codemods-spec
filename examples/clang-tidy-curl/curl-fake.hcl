# Credential-free variant of curl.hcl. It runs the same public,
# cross-platform clang-tidy campaign, but records review state in a local JSON
# file instead of opening GitHub PRs.

codemod "curl-tidy-braces-fake" {
  description = "local fake-review run of clang-tidy readability-braces-around-statements over curl lib/vauth"

  author      = "aozgaa@gmail.com"
  repo        = "../../../curl"
  base_branch = "master"

  decomposition {
    type    = "glob"
    include = ["lib/vauth/*.c"]
    exclude = ["lib/vauth/digest.c"]
    kind    = "file"
  }

  run     = "./mods/clang-tidy-fix.sh"
  postmod = "./mods/build-and-test.sh"
  workdir = "./work-fake"

  review {
    driver = "fake"
    repo   = "./work-fake/prs.json"
    title  = "[codemods] {codemod}: {unit}"
    body   = "Automated `clang-tidy -fix` (readability-braces-around-statements) over `{unit}`, applied and compile-verified by codemods."
  }
}
