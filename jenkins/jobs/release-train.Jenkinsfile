// release-train (demo 21): its timer ticks every half hour, every day, and the
// train leaves at the departures services.yaml names (train.departures, by
// default 14:00 Europe/Amsterdam) on its days (train.days, by default
// weekdays), when scripts/demo.sh up release-train has switched it on.
// scripts/demos/release-train/train.py decides: nothing new,
// an unfinished issue without a flag that's off, or a departure that starts
// the release job as release-bot. A tick that isn't a departure ends at once,
// not built. By hand, REHEARSAL leaves now, whatever the time.

pipeline {
    agent { label 'tools-host' }
    options {
        timeout(time: 10, unit: 'MINUTES')
    }
    stages {
        stage('Train') {
            steps {
                script {
                    boolean timer = currentBuild.getBuildCauses('hudson.triggers.TimerTrigger$TimerTriggerCause') as boolean
                    if (!timer && !params.REHEARSAL) {
                        error 'By hand, the train leaves only as a rehearsal: tick REHEARSAL.'
                    }
                    int code = sh(returnStatus: true,
                                  script: "python3 scripts/demos/release-train/train.py ${timer ? '--timer' : '--rehearsal'}")
                    if (code == 3) {
                        currentBuild.result = 'NOT_BUILT'
                        currentBuild.description = readFile('.release/train.txt').trim()
                    } else if (code != 0) {
                        error "train.py exited with ${code}: its log says why."
                    } else {
                        currentBuild.description = readFile('.release/train.txt').trim()
                    }
                }
            }
        }
    }
}
