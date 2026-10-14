--- server/README.md
+++ server/README.md (expected)
@@ -221,5 +221,3 @@
 Generation writes only the missing modules of `services`: a new router group gets its module with stubs, and the module of a renamed or removed group stays for you to move or delete.
 Each generation prints the `uv add` command that adds the runtime dependencies of the package to your project;
 run it again when a new version of datamodel-code-generator changes them.
-
-Changed documentation.
