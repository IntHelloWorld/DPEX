#!/usr/bin/env bash

export DEFECTS4J_HOME="/config/mllm/defects4j"
export JAVA_HOME="/usr/lib/jvm/java-11-openjdk-amd64"
export TZ="America/Los_Angeles"

export PATH="/config/perl5/bin:${DEFECTS4J_HOME}/framework/bin:${JAVA_HOME}/bin:${PATH}"
export PERL5LIB="/config/perl5/lib/perl5${PERL5LIB:+:${PERL5LIB}}"
export PERL_LOCAL_LIB_ROOT="/config/perl5${PERL_LOCAL_LIB_ROOT:+:${PERL_LOCAL_LIB_ROOT}}"
export PERL_MB_OPT='--install_base "/config/perl5"'
export PERL_MM_OPT="INSTALL_BASE=/config/perl5"
