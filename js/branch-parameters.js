/* global angular */
angular.module('branchtablesinitialization.parameters', [])
    .controller('BranchTablesParametersController', ['$scope', function ($scope) {
        if (typeof $scope.config.dry_run === 'undefined') {
            $scope.config.dry_run = true;
        }
        if (typeof $scope.config.overwrite_existing === 'undefined') {
            $scope.config.overwrite_existing = false;
        }
        $scope.$watch('config.dry_run', function (dryRun) {
            if (dryRun) {
                $scope.config.overwrite_existing = false;
            }
        });
        $scope.choices = [];
        $scope.warnings = [];
        $scope.loadBranches = function () {
            $scope.loading = true;
            $scope.error = null;
            $scope.masterKey = null;
            $scope.choices = [];
            $scope.warnings = [];
            return $scope.callPythonDo({}).then(function (data) {
                $scope.masterName = data.master_name;
                $scope.masterKey = data.master_key;
                $scope.choices = data.choices;
                $scope.warnings = data.warnings;
                if (!$scope.choices.some(function (choice) {
                    return choice.value === $scope.config.branch_project_key;
                })) {
                    $scope.config.branch_project_key = $scope.choices.some(function (choice) {
                        return choice.value === data.master_key;
                    }) ? data.master_key : null;
                }
                $scope.loading = false;
            }, function () {
                $scope.config.branch_project_key = null;
                $scope.error = 'Unable to load the master project and branches. Check project access and try refreshing.';
                $scope.loading = false;
            });
        };
        $scope.loadBranches();
    }]);
