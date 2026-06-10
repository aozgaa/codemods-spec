We would like to write up a complete specification for a system called "codemods".
The idea is to write a specification as well as sample implementation for how such a system would work, and then allow
folks in different enterprises with their own software-development-lifecycle stacks to implement the system so it works
with their tools.

The concept of codemods is to specify a system that allows for generating and splitting up big changes like refactors
into smaller units of code that can be reviewed individually. Think of an enterprise with 1000+ technical staff.

For example, imagine we have a clang-tidy refactor we would like to apply over a codebase, but if we do so, the
resulting code review will have 100+ required reviewers.
Instead we would like to break up the refactor so it goes one code-owner group at a time.

The tool should be able to:
1) have a database (eg: redis/pgsql/sqlite) to track the lifecycle/state of refactor subtasks
2) have a task decomposition (eg: run over this list of files, run over this list of build targets, run over this list
   of git submodules, run over this list of codeowners -> {file} mappings)
3) fetch or otherwise prepare worktrees or dev environments to "run the refactor"
4) execute "codemods" which as shell scripts that exit 0 on success and possibly apply diffs to a repo.
   The codemods themselves take a single argument (eg: a filename, a library target, ...)
5) execute "postmod" tasks (like ensure the code still builds and passes tests, the set of tests to run either being an
   entire test suite or maybe derived from some kind of repo config (this would be bespoke logive, probably also encoded
   in a shell script)
6) track successes/failures in the database and have some configurable policy/connectors for author notification (eg:
   email, slack, update task in task trackers like jira/asana/linear/...)
7) have some mechanism for opening up pr's (against whatever code review tool the enterprise uses)
8) track the code review lifecycle open/merged/cancelled and specify what to do based on each state (more hooks).

Different aspects of the system will be specific to the enterprise/eng/org:
1) monorepo or multirepo
2) version control system: git,svn,fossil,...
3) database: pgsql,redis,sqlite,...
4) code review system: github,forgejo,src.ht,...
5) notification system: email,slack,...
6) worktree preparation: git clone, git worktree new, some git worktree pool, vm preparation, docker container prep, ...
7) task decomposition (file globs, codeowners, ...)

There should be a general SPEC.md which discusses the overall philosophy, and then a more specific/prescriptive
EXAMPLE_SPEC.md attached to the example implementation which is very concrete about design details (like db schema /
state transitions / ...). The high level spec is comparable to something like https://github.com/openai/symphony .

====================

The sample implementation should:
1) work with github via `gh` subcommands (eg: prompt user to `gh login auth` on initial setup)
2) make worktrees by cloning locally (eg: `git clone /path/to/repo /path/to/worktrees/codemod-<name>-001`)
3) use email delivery for success/failures,
4) use pgsql for the codemod state database
5) apply refactors to a public, cross-platform sample repository.
6) be implemented in python, using `pixi` for dependencies/build/runnning.

It should be exercised by working against a user-ownder fork of the curl project: https://github.com/curl/curl .
Some shell scripts to init or clean a fork should be included in the sample scripts.

For an example codemod we can do a `run-clang-tidy -p build/ -fix` over each subdir of `src` separately:
```
% ls -la ../curl/src/*.h | head
-rw-r--r--  1 art  staff   1344 Jun  9 22:07 ../curl/src/config2setopts.h
-rw-r--r--  1 art  staff   1787 Jun  9 22:07 ../curl/src/slist_wc.h
-rw-r--r--  1 art  staff   1209 Jun  9 22:07 ../curl/src/terminal.h
-rw-r--r--  1 art  staff   1352 Jun  9 22:07 ../curl/src/tool_cb_dbg.h
-rw-r--r--  1 art  staff   2147 Jun  9 22:07 ../curl/src/tool_cb_hdr.h
-rw-r--r--  1 art  staff   1800 Jun  9 22:07 ../curl/src/tool_cb_prg.h
-rw-r--r--  1 art  staff   1528 Jun  9 22:07 ../curl/src/tool_cb_rea.h
-rw-r--r--  1 art  staff   1285 Jun  9 22:07 ../curl/src/tool_cb_see.h
-rw-r--r--  1 art  staff   1413 Jun  9 22:07 ../curl/src/tool_cb_soc.h
-rw-r--r--  1 art  staff   1477 Jun  9 22:07 ../curl/src/tool_cb_wrt.h
```

We need a configuration format for the codemods.
Some kind of simple/widely available config format is preferable. Options include hcl,toml,json,...
The config language should provide for things like
```
decomposition {
  type = "literal"
  items = ["src/app", "src/lib", "src/tools"]
}

decomposition {
  type = "glob"
  include = ["src/*"]
  kind    = "directory"
}

decomposition {
  type = "glob"
  include = ["src/*.h"]
  kind    = "file"
}

decomposition {
  type = "command"

  command = <<-EOF
    find src -maxdepth 1 -mindepth 1 -type d -print0
  EOF

  format = "nul"
}

decomposition {
  type    = "command"
  command = "find src -maxdepth 1 -mindepth 1 -type d -print0"
  format  = "nul"
}

decomposition {
  type = "codeowners"
  path = ".github/CODEOWNERS"
}
```
for the task decomposition into separate command invocations.

As you can see, directories or owners might pop into/out of existence across multiple invocations.
There should be some kind of functionality (eg: a crud gui or just some command wrappers) to inspect the state of the db
and check for the health and maybe doctor/repair the task state (eg: close orphaned prs / subtasks).
