# Public Courses on Omegaup
This repository contains the course content for the public courses on Omegaup.

## How to Contribute
You can contribute to the courses by adding new content, fixing typos, or improving existing materials. To do so, please follow these steps:
1. Fork the repository.
2. Create a new branch for your changes.
3. Make your changes and commit them with a clear message.
4. Push your changes to your forked repository.
5. Create a pull request to the main repository.

## How to Sync Courses:
If you think that a course content in this repository has ran out of sync with the omegaup.com you can raise a pull request to sync the content.
To do this, follow these steps:
1. Fork the repository.
2. Create a new branch for your changes.
3. Edit the `sync-course.json` file to include the course name and the specific content that is out of sync.
4. Commit your changes with a clear message.
5. Push your changes to your forked repository.
6. Create a pull request to the main repository with target branch sync-course.
7. When your pull request is merged a github action will run to sync the course content with omegaup.com and add commit to your pull request.
8. Now the admins will merge this pull request to the main branch.